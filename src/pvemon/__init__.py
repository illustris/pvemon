# from prometheus_client import start_http_server, Gauge, Info, REGISTRY, Metric
from prometheus_client.core import InfoMetricFamily, GaugeMetricFamily, CounterMetricFamily, REGISTRY
from prometheus_client.registry import Collector
from prometheus_client import start_http_server

import psutil
import time
import argparse
import re
import itertools
import os

import pexpect

import logging
import cProfile

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pvecommon
import qmblock

DEFAULT_PORT = 9116
DEFAULT_INTERVAL = 10
DEFAULT_PREFIX = "pve"

gauge_settings = [
    ('kvm_cpu', 'CPU time for VM', ['id', 'mode']),
    ('kvm_vcores', 'vCores allocated to the VM', ['id']),
    ('kvm_maxmem', 'Maximum memory (bytes) allocated to the VM', ['id']),
    ('kvm_memory_percent', 'Percentage of host memory used by VM', ['id']),
    ('kvm_memory_extended', 'Detailed memory metrics for VM', ['id', 'type']),
    ('kvm_threads', 'Threads used by the KVM process', ['id']),
    ('kvm_io_read_count', 'Number of read system calls made by the KVM process', ['id']),
    ('kvm_io_read_bytes', 'Number of bytes read from disk', ['id']),
    ('kvm_io_read_chars', 'Number of bytes read including buffers', ['id']),
    ('kvm_ctx_switches', 'Context switches', ['id', 'type']),
    ('kvm_io_write_count', 'Number of write system calls made by the KVM process', ['id']),
    ('kvm_io_write_bytes', 'Number of bytes written to disk', ['id']),
    ('kvm_io_write_chars', 'Number of bytes written including buffers', ['id']),

    ('kvm_nic_queues', 'Number of queues in multiqueue config', ['id', 'ifname']),

    ('kvm_disk_size', 'Size of virtual disk', ['id', 'disk_name']),
]

label_flags = [ "-id", "-name", "-cpu" ]
get_label_name = lambda flag: flag[1:]
info_settings = [
    ('kvm', 'information for each KVM process'),
]

flag_to_label_value = lambda args, match: next((args[i+1] for i, x in enumerate(args[:-1]) if x == match), "unknown").split(",")[0]

def parse_mem(cmdline):
    ret = flag_to_label_value(cmdline, "-m")
    # lazy way to detect NUMA
    # the token after -m might look something like 'size=1024,slots=255,maxmem=4194304M'
    if ret.isnumeric():
        return int(ret)*1024

    # probably using NUMA
    ret = 0
    for arg in cmdline:
        if "memory-backend-ram" in arg:
            assert(arg[-1]=='M')
            ret += 1024*int(arg.split("=")[-1][:-1])
    return ret

def create_or_get_gauge(metric_name, labels, dynamic_gauges, gauge_lock):
    with gauge_lock:
        if metric_name not in dynamic_gauges:
            dynamic_gauges[metric_name] = GaugeMetricFamily(f"{prefix}_{metric_name}", f'{metric_name} for KVM process', labels=labels)
    return dynamic_gauges[metric_name]

def create_or_get_info(info_name, labels, dynamic_infos, info_lock):
    with info_lock:
        if (info_name,str(labels)) not in dynamic_infos:
            dynamic_infos[(info_name,str(labels))] = InfoMetricFamily(f"{prefix}_{info_name}", f'{info_name} for {str(labels)}', labels=labels)
    return dynamic_infos[(info_name,str(labels))]

def get_memory_info(pid):
    metrics = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith(("Vm", "Rss", "Hugetlb")):
                key, value, unit = line.split()
                if unit == "kB":
                    metrics[key.lower()] = int(value) * 1024  # convert KB to bytes
    return metrics


def extract_nic_info_from_monitor(vm_id):
    raw_output = pvecommon.qm_term_cmd(vm_id, 'info network')

    nic_info_list = re.findall(r'(net\d+:.*?)(?=(net\d+:|$))', raw_output, re.S)

    nics_map = {}

    for netdev, cfg in [x.strip().split(": ") for x in re.findall(r'(net\d+:.*?)(?:\r{0,2}\n|(?=\s*\\ net\d+:)|$)', raw_output, re.S)]:
        for cfg_pair in cfg.split(","):
            if cfg_pair=='':
                continue
            key, value = cfg_pair.split('=')
            if netdev not in nics_map:
                nics_map[netdev] = {}
            nics_map[netdev][key] = value

    return [
        {
            "netdev": netdev,
            "queues": int(cfg["index"])+1,
            "type": cfg["type"],
            "model": cfg["model"],
            "macaddr": cfg["macaddr"],
            "ifname": cfg["ifname"]
        }
        for netdev, cfg in nics_map.items()
    ]

def read_interface_stats(ifname):
    stats_dir = f"/sys/class/net/{ifname}/statistics/"
    stats = {}
    try:
        for filename in os.listdir(stats_dir):
            with open(f"{stats_dir}{filename}", "r") as f:
                stats[filename] = int(f.read().strip())
    except FileNotFoundError:
        pass
    return stats

def collect_kvm_metrics():
    logging.debug("collect_kvm_metrics() called")
    gauge_dict = {}
    info_dict = {}
    for name, description, labels in gauge_settings:
        gauge_dict[name] = GaugeMetricFamily(f"{prefix}_{name}", description, labels=labels)

    for name, description in info_settings:
        info_dict[name] = InfoMetricFamily(f"{prefix}_{name}", description)

    dynamic_gauges = {}
    gauge_lock = Lock() # avoid race condition when checking and creating gauges
    dynamic_infos = {}
    info_lock = Lock() # avoid race condition when checking and creating infos

    procs = []
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline', 'cpu_percent', 'memory_percent', 'num_threads']):
        try:
            if proc.info['exe'] == '/usr/bin/qemu-system-x86_64':
                vmid = flag_to_label_value(proc.info['cmdline'], "-id")
                # Check if VM definition exists. If it is missing, qm commands will fail.
                # VM configs are typically missing when a VM is migrating in.
                # The config file is moved after the drives and memory are synced.
                if not os.path.exists(f'/etc/pve/qemu-server/{vmid}.conf'):
                    continue
                procs.append(
                    (
                        proc,
                        proc.info['cmdline'],
                        vmid
                    )
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Try to find ID to pool mapping here

    for proc, cmdline, id in procs:
        # Extract vm labels from cmdline
        info_label_dict = {get_label_name(l): flag_to_label_value(cmdline,l) for l in label_flags}
        info_label_dict['pid']=str(proc.pid)
        logging.debug(f"got PID: {proc.pid}")
        info_dict["kvm"].add_metric([], info_label_dict)

        d = {
            "kvm_vcores": flag_to_label_value(cmdline,"-smp"),
            "kvm_maxmem": parse_mem(cmdline),
            "kvm_memory_percent": proc.info['memory_percent'],
            "kvm_threads": proc.info['num_threads'],
        }

        for k, v in d.items():
            gauge_dict[k].add_metric([id], v)
            logging.debug(f"gauge_dict[{k}].labels(id={id}).set({v})")

        cpu_times = proc.cpu_times()
        for mode in ['user', 'system', 'iowait']:
            gauge_dict["kvm_cpu"].add_metric([id, mode], getattr(cpu_times,mode))

        io = proc.io_counters()
        for io_type, attr in itertools.product(['read', 'write'], ['count', 'bytes', 'chars']):
            gauge_dict[f'kvm_io_{io_type}_{attr}'].add_metric([id], getattr(io, f"{io_type}_{attr}"))

        for type in [ "voluntary", "involuntary" ]:
            gauge_dict["kvm_ctx_switches"].add_metric([id, type], getattr(proc.num_ctx_switches(),type))

        memory_metrics = get_memory_info(proc.pid)  # Assuming proc.pid gives you the PID of the process
        for key, value in memory_metrics.items():
            gauge_dict["kvm_memory_extended"].add_metric([id, key], value)

    # upper limit on max_workers for safety
    with ThreadPoolExecutor(max_workers=16) as executor:
        def map_netstat_proc(id):
            for nic_info in extract_nic_info_from_monitor(id):
                queues = nic_info["queues"]
                del nic_info["queues"]
                nic_labels = {"id": id, "ifname": nic_info["ifname"]}
                prom_nic_info = create_or_get_info("kvm_nic", nic_labels.keys(), dynamic_infos, info_lock)

                prom_nic_info.add_metric(nic_labels.values(), nic_info)

                gauge_dict["kvm_nic_queues"].add_metric(nic_labels.values(), queues)

                interface_stats = read_interface_stats(nic_info["ifname"])
                for filename, value in interface_stats.items():
                    metric_name = f"kvm_nic_{filename}"
                    gauge = create_or_get_gauge(metric_name, nic_labels.keys(), dynamic_gauges, gauge_lock)
                    gauge.add_metric(nic_labels.values(), value)

        def map_disk_proc(id):
            for disk_name, disk_info in qmblock.extract_disk_info_from_monitor(id).items():
                logging.debug(f"map_disk_proc: {disk_name=}, {disk_info=}")
                disk_labels = {"id": id, "disk_name": disk_name}
                prom_disk_info = create_or_get_info("kvm_disk", disk_labels.keys(), dynamic_infos, info_lock)
                prom_disk_info.add_metric(disk_labels.values(), disk_info)
                disk_size = qmblock.get_disk_size(disk_info["disk_path"], disk_info["disk_type"])
                if disk_size == None and disk_info["disk_type"] != "qcow2":
                    logging.debug(f"collect_kvm_metrics: failed to get disk size for {disk_info=}")
                else:
                    gauge_dict["kvm_disk_size"].add_metric([id, disk_name], qmblock.get_disk_size(disk_info["disk_path"], disk_info["disk_type"]))


        list(executor.map(map_netstat_proc, [ proc[2] for proc in procs ]))
        list(executor.map(map_disk_proc, [ proc[2] for proc in procs ]))

    for v in info_dict.values():
        yield v
    for v in dynamic_infos.values():
        yield v
    for v in gauge_dict.values():
        yield v
    for v in dynamic_gauges.values():
        yield v
    logging.debug("collect_kvm_metrics() return")

class PVECollector(object):
    def __init__(self):
        return

    def collect(self):
        if cli_args.collect_running_vms.lower() == 'true':
            for x in collect_kvm_metrics():
                yield x

def main():
    parser = argparse.ArgumentParser(description='PVE metrics exporter for Prometheus')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='Port for the exporter to listen on')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='THIS OPTION DOES NOTHING')
    parser.add_argument('--collect-running-vms', type=str, default='true', help='Enable or disable collecting running VMs metric (true/false)')
    parser.add_argument('--metrics-prefix', type=str, default=DEFAULT_PREFIX, help='<prefix>_ will be prepended to each metric name')
    parser.add_argument('--loglevel', type=str, default='INFO', help='Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    parser.add_argument('--profile', type=str, default='false', help='collect metrics once, and print profiling stats')
    parser.add_argument('--qm-terminal-timeout', type=int, default=10, help='timeout for qm terminal commands')
    parser.add_argument('--qm-max-ttl', type=int, default=600, help='cache ttl for data pulled from qm monitor')
    parser.add_argument('--qm-rand', type=int, default=60, help='randomize qm monitor cache expiry')
    parser.add_argument('--qm-monitor-defer-close', type=str, default="true", help='defer and retry closing unresponsive qm monitor sessions')

    args = parser.parse_args()
    global cli_args
    cli_args = args

    loglevel = getattr(logging, args.loglevel.upper(), None)
    if not isinstance(loglevel, int):
        raise ValueError(f'Invalid log level: {args.loglevel}')
    logging.basicConfig(level=loglevel,format='%(asctime)s: %(message)s')

    global prefix
    prefix = args.metrics_prefix
    pvecommon.global_qm_timeout = args.qm_terminal_timeout
    pvecommon.qm_max_ttl = args.qm_max_ttl
    pvecommon.qm_rand = args.qm_rand
    pvecommon.qm_monitor_defer_close = args.qm_monitor_defer_close

    if args.profile.lower() == 'true':
        profiler = cProfile.Profile()
        profiler.enable()
        collect_kvm_metrics()
        profiler.disable()
        profiler.print_stats(sort='cumulative')
        return
    else:
        REGISTRY.register(PVECollector())
        start_http_server(args.port)

    while True:
        time.sleep(100)

if __name__ == "__main__":
    main()
