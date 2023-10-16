import pexpect
import re
import os

import pvecommon

def get_device(disk_path):
    try:
        return os.readlink(disk_path).split('/')[-1]
    except OSError:
        return None

def extract_disk_info_from_monitor(vm_id):
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
        if "/dev/zvol" in disk_path:
            disks_map[disk_name]["disk_type"] = "zvol"
            disks_map[disk_name]["pool"] = "/".join(disk_path.split("/")[3:-1])
            disks_map[disk_name]["vol_name"] = disk_path.split("/")[-1]
            disks_map[disk_name]["device"] = get_device(disk_path)
        elif re.match(r'/dev/[^/]+/vm-\d+-disk-\d+', disk_path): # lvm
            disks_map[disk_name]["disk_type"] = "lvm"
            vg_name, vol_name = re.search(r'/dev/([^/]+)/(vm-\d+-disk-\d+)', disk_path).groups()
            disks_map[disk_name]["vg_name"] = vg_name
            disks_map[disk_name]["vol_name"] = vol_name
            disks_map[disk_name]["device"] = get_device(disk_path)
        elif "/dev/rbd-pve" in disk_path: # rbd
            disks_map[disk_name]["disk_type"] = "rbd"
            rbd_parts = disk_path.split('/')
            disks_map[disk_name]["cluster_id"] = rbd_parts[-3]
            disks_map[disk_name]["pool_name"] = rbd_parts[-2]
            disks_map[disk_name]["vol_name"] = rbd_parts[-1]
            disks_map[disk_name]["device"] = get_device(disk_path)
        elif "/dev/rbd-pve" in disk_path:
            disks_map[disk_name]["disk_type"] = "rbd"
            rbd_parts = disk_path.split('/')
            pool_name = rbd_parts[-3]
            vm_id = rbd_parts[-1].split('-')[1]
            disk_number = rbd_parts[-1].split('-')[-1]
            disks_map[disk_name]["pool_name"] = pool_name
            disks_map[disk_name]["rbd_vm_id"] = vm_id
            disks_map[disk_name]["rbd_disk_number"] = disk_number
        for line in data[1:-1]:
            if "Attached to" in line:
                attached_to = line.split(":")[-1].strip()
                if "virtio" in attached_to:
                    attached_to=attached_to.split("/")[3]
                disks_map[disk_name]["attached_to"] = attached_to
            if "Cache mode" in line:
                for cache_mode in line.split(":")[-1].strip().split(", "):
                    disks_map[disk_name][f"cache_mode_{cache_mode}"] = "true"
            if "Detect zeroes" in line:
                disks_map[disk_name]["detect_zeroes"] = "on"
    return disks_map

if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(extract_disk_info_from_monitor(sys.argv[1])))
