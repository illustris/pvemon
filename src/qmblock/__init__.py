import pexpect
import re
import os
import json
import stat

import pvecommon

extract_disk_info_max_retries = 1

def get_device(disk_path):
    try:
        return os.readlink(disk_path).split('/')[-1]
    except OSError:
        return None

def handle_json_path(path):
    def search_dict(dictionary):
        if 'driver' in dictionary and dictionary['driver'] == 'host_device' and 'filename' in dictionary:
            return dictionary['filename']
        for key, value in dictionary.items():
            if isinstance(value, dict):
                result = search_dict(value)
                if result:
                    return result
        return None
    filename = search_dict(json.loads(path[5:]))
    if filename is None:
        raise ValueError('No host_device driver found or filename is missing')
    return filename

def extract_disk_info_from_monitor(vm_id, retries = 0):
    raw_output = pvecommon.qm_term_cmd(vm_id, 'info block')
    disks_map = {}
    disks = [x.strip() for x in raw_output.split("drive-")[1:]]
    for disk in disks:
        data = [x.strip() for x in disk.split("\n")]
        pattern = r'(\w+) \(#block(\d+)\): (.+) \(([\w, -]+)\)'
        match = re.match(pattern, data[0])
        if not match:
            continue

        disk_name, block_id, disk_path, disk_type_and_mode = match.groups()
        disk_type = disk_type_and_mode.split(", ")[0]
        if "efidisk" in disk_name: # TODO: handle this later
            continue

        if disk_path.startswith("json:"):
            disk_path = handle_json_path(disk_path)

        disks_map[disk_name]={
            "disk_name": disk_name,
            "block_id": block_id,
            "disk_path": disk_path,
            "disk_type": disk_type,
        }
        if "read-only" in disk_type_and_mode:
            disks_map[disk_name]["read_only"] = "true"
        if disk_type == "qcow2":
            disks_map[disk_name]["vol_name"] = disk_path.split("/")[-1].split(".")[0]
        if disk_path.startswith("/dev/zvol"): # zfs
            disks_map[disk_name]["disk_type"] = "zvol"
            disks_map[disk_name]["pool"] = "/".join(disk_path.split("/")[3:-1])
            disks_map[disk_name]["vol_name"] = disk_path.split("/")[-1]
            disks_map[disk_name]["device"] = get_device(disk_path)
        elif disk_path.startswith("/dev/rbd-pve"): # rbd
            disks_map[disk_name]["disk_type"] = "rbd"
            rbd_parts = disk_path.split('/')
            disks_map[disk_name]["cluster_id"] = rbd_parts[-3]
            disks_map[disk_name]["pool"] = rbd_parts[-2]
            # Keeping for backwards compatibility
            disks_map[disk_name]["pool_name"] = rbd_parts[-2]
            disks_map[disk_name]["vol_name"] = rbd_parts[-1]
            disks_map[disk_name]["device"] = get_device(disk_path)
        elif re.match(r'/dev/[^/]+/vm-\d+-disk-\d+', disk_path): # lvm
            disks_map[disk_name]["disk_type"] = "lvm"
            vg_name, vol_name = re.search(r'/dev/([^/]+)/(vm-\d+-disk-\d+)', disk_path).groups()
            disks_map[disk_name]["vg_name"] = vg_name
            disks_map[disk_name]["vol_name"] = vol_name
            disks_map[disk_name]["device"] = get_device(disk_path)
        # At this point, if disks_map[disk_name]["device"] exists and is None, the cache might be stale
        # Flush the cache for this VMID and try again
        if "device" in disks_map[disk_name] and disks_map[disk_name]["device"] == None and retries < extract_disk_info_max_retries:
            pvecommon.qm_term_cmd.invalidate_cache(vm_id, 'info block')
            return extract_disk_info_from_monitor(vm_id, retries+1)
        for line in data[1:-1]:
            if "Attached to" in line:
                attached_to = line.split(":")[-1].strip()
                if "virtio" in attached_to:
                    attached_to=attached_to.split("/")[3]
                disks_map[disk_name]["attached_to"] = attached_to
            if "Cache mode" in line:
                for cache_mode in line.split(":")[-1].strip().split(", "):
                    cache_mode_nospace = "_".join(cache_mode.split())
                    disks_map[disk_name][f"cache_mode_{cache_mode_nospace}"] = "true"
            if "Detect zeroes" in line:
                disks_map[disk_name]["detect_zeroes"] = "on"
    return disks_map

def get_disk_size(disk_path, disk_type):
    if stat.S_ISBLK(os.stat(disk_path).st_mode):
        disk_name = os.path.basename(os.path.realpath(disk_path))
        size_file_path = f"/sys/block/{disk_name}/size"
        sector_size_file_path = f"/sys/block/{disk_name}/queue/hw_sector_size"

        with open(size_file_path, 'r') as f:
            sectors = int(f.read().strip())

            with open(sector_size_file_path, 'r') as sector_size_file:
                sector_size = int(sector_size_file.read().strip())

                size_in_bytes = sectors * sector_size
    else:
        size_in_bytes = os.path.getsize(disk_path)

    return size_in_bytes

if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(extract_disk_info_from_monitor(sys.argv[1])))
