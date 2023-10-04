from prometheus_client import start_http_server, Gauge, Info
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
]

gauge_dict = {}
info_dict = {}

label_flags = [ "-id", "-name", "-cpu" ]
get_label_name = lambda flag: flag[1:]
info_settings = [
    ('kvm', 'information for each KVM process'),
]

flag_to_label_value = lambda args, match: next((args[i+1] for i, x in enumerate(args[:-1]) if x == match), "unknown").split(",")[0]

dynamic_gauges = {}
gauge_lock = Lock() # avoid race condition when checking and creating gauges
def create_or_get_gauge(metric_name, labels):
    with gauge_lock:
        if metric_name not in dynamic_gauges:
            dynamic_gauges[metric_name] = Gauge(f"{prefix}_{metric_name}", f'{metric_name} for KVM process', labels)
    return dynamic_gauges[metric_name]

dynamic_infos = {}
info_lock = Lock() # avoid race condition when checking and creating infos
def create_or_get_info(info_name, labels):
    with info_lock:
        if (info_name,str(labels)) not in dynamic_infos:
            dynamic_infos[(info_name,str(labels))] = Info(f"{prefix}_{info_name}", f'{info_name} for {str(labels)}', labels)
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
    child = pexpect.spawn(f'qm monitor {vm_id}')
    
    # Wait for the QEMU monitor prompt
    child.expect('qm>', timeout=10)
    
    # Execute 'info network'
    child.sendline('info network')
    
    # Wait for the prompt again
    child.expect('qm>', timeout=10)
    
    # Parse the output
    raw_output = child.before.decode('utf-8').strip()
    child.close()
    nic_info_list = re.findall(r'(net\d+:.*?)(?=(net\d+:|$))', raw_output, re.S)

    nics_map = {}

    for netdev, cfg in [x.strip().split(": ") for x in re.findall(r'[^\n]*(net\d+:[^\n]*)\n', raw_output, re.S)]:
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
    procs = [
        (
            # proc object
            proc,
            # cmdline list
            proc.cmdline(),
            # VMID
            flag_to_label_value(proc.cmdline(),"-id")
        )
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'memory_percent', 'num_threads'])
        if proc.info['name'] == 'kvm'
    ]
    for proc, cmdline, id in procs:
        # Extract vm labels from cmdline
        info_label_dict = {get_label_name(l): flag_to_label_value(cmdline,l) for l in label_flags}
        info_label_dict['pid']=str(proc.pid)
        logging.debug(f"got PID: {proc.pid}")
        info_dict["kvm"].info(info_label_dict)

        d = {
            "kvm_vcores": flag_to_label_value(cmdline,"-smp"),
            "kvm_maxmem": int(flag_to_label_value(cmdline,"-m"))*1024,
            "kvm_memory_percent": proc.info['memory_percent'],
            "kvm_threads": proc.info['num_threads'],
        }

        for k, v in d.items():
            gauge_dict[k].labels(id=id).set(v)
            logging.debug(f"gauge_dict[{k}].labels(id={id}).set({v})")

        cpu_times = proc.cpu_times()
        for mode in ['user', 'system', 'iowait']:
            gauge_dict["kvm_cpu"].labels(id=id, mode=mode).set(getattr(cpu_times, mode))

        io = proc.io_counters()
        for io_type, attr in itertools.product(['read', 'write'], ['count', 'bytes', 'chars']):
            gauge = globals()["gauge_dict"][f'kvm_io_{io_type}_{attr}']
            gauge.labels(id=id).set(getattr(io, f"{io_type}_{attr}"))

        for type in [ "voluntary", "involuntary" ]:
            gauge_dict["kvm_ctx_switches"].labels(id=id, type=type).set(getattr(proc.num_ctx_switches(),type))

        memory_metrics = get_memory_info(proc.pid)  # Assuming proc.pid gives you the PID of the process
        for key, value in memory_metrics.items():
            gauge_dict["kvm_memory_extended"].labels(id=id, type=key).set(value)

    # upper limit on max_workers for safety
    with ThreadPoolExecutor(max_workers=16) as executor:
        def map_netstat_proc(id):
            for nic_info in extract_nic_info_from_monitor(id):
                queues = nic_info["queues"]
                del nic_info["queues"]
                nic_labels = {"id": id, "ifname": nic_info["ifname"]}
                prom_nic_info = create_or_get_info("kvm_nic", nic_labels.keys())
                prom_nic_info.labels(**nic_labels).info({k: v for k, v in nic_info.items() if k not in nic_labels.keys()})

                gauge_dict["kvm_nic_queues"].labels(**nic_labels).set(queues)

                interface_stats = read_interface_stats(nic_info["ifname"])
                for filename, value in interface_stats.items():
                    metric_name = f"kvm_nic_{filename}"
                    gauge = create_or_get_gauge(metric_name, nic_labels.keys())
                    gauge.labels(**nic_labels).set(value)

        def map_disk_proc(id):
            for disk_name, disk_info in qmblock.extract_disk_info_from_monitor(id).items():
                disk_labels = {"id": id, "disk_name": disk_name}
                prom_disk_info = create_or_get_info("kvm_disk", disk_labels.keys())
                prom_disk_info.labels(**disk_labels).info({k: v for k, v in disk_info.items() if k not in disk_labels.keys()})

        list(executor.map(map_netstat_proc, [ proc[2] for proc in procs ]))
        list(executor.map(map_disk_proc, [ proc[2] for proc in procs ]))

def main():
    parser = argparse.ArgumentParser(description='PVE metrics exporter for Prometheus')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='Port for the exporter to listen on')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='Interval between metric collections in seconds')
    parser.add_argument('--collect-running-vms', type=str, default='true', help='Enable or disable collecting running VMs metric (true/false)')
    parser.add_argument('--metrics-prefix', type=str, default=DEFAULT_PREFIX, help='<prefix>_ will be prepended to each metric name')
    parser.add_argument('--loglevel', type=str, default='INFO', help='Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    parser.add_argument('--profile', type=str, default='false', help='collect metrics once, and print profiling stats')

    args = parser.parse_args()

    loglevel = getattr(logging, args.loglevel.upper(), None)
    if not isinstance(loglevel, int):
        raise ValueError(f'Invalid log level: {args.loglevel}')
    logging.basicConfig(level=loglevel,format='%(asctime)s: %(message)s')

    global prefix
    prefix = args.metrics_prefix

    for name, description, labels in gauge_settings:
        gauge_dict[name] = Gauge(f"{prefix}_{name}", description, labels)

    for name, description in info_settings:
        info_dict[name] = Info(f"{prefix}_{name}", description)

    start_http_server(args.port)

    if args.profile.lower() == 'true':
        profiler = cProfile.Profile()
        profiler.enable()
        collect_kvm_metrics()
        profiler.disable()
        profiler.print_stats(sort='cumulative')
        return

    while True:
        if args.collect_running_vms.lower() == 'true':
            collect_kvm_metrics()
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
