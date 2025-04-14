import os
import re
import logging
import pprint

from prometheus_client.core import InfoMetricFamily, GaugeMetricFamily, CounterMetricFamily, REGISTRY

gauge_settings = [
    ('node_storage_size', 'Size of the storage pool. This number is inaccurate for ZFS.', ['name', 'type']),
    ('node_storage_free', 'Free space on the storage pool', ['name', 'type'])
]

info_settings = [
    ('node_storage', 'information for each PVE storage'),
]

# Sanitize the key to match Prometheus label requirements
# Replace any character that is not a letter, digit, or underscore with an underscore
sanitize_key = lambda key: re.sub(r"[^a-zA-Z0-9_]", "_", key)

_cached_storage_data = None
_cached_mtime = None

def parse_storage_cfg(file_path='/etc/pve/storage.cfg'):
    logging.debug(f"parse_storage_cfg({file_path=}) called")
    global _cached_storage_data, _cached_mtime

    # Check if file exists
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"The file {file_path} does not exist.")

    # Get the file's modification time
    current_mtime = os.path.getmtime(file_path)

    # If the data is already cached and the file hasn't changed, return the cached data
    if _cached_storage_data is not None and _cached_mtime == current_mtime:
        logging.debug("parse_storage_cfg: returning cached data")
        return _cached_storage_data

    logging.debug("parse_storage_cfg: file modified, dropping cache")

    # Initialize list to store storages
    storage_list = []
    current_storage = None

    with open(file_path, 'r') as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#"):
                # Ignore empty lines or comments
                continue

            if ":" in line:
                # If we were processing a previous section, append it to the list
                if current_storage:
                    storage_list.append(current_storage)

                # Start a new storage definition
                section_type, section_name = line.split(":", 1)
                current_storage = {
                    'type': sanitize_key(section_type.strip()),  # Sanitize the section type
                    'name': sanitize_key(section_name.strip()),  # Sanitize the section name
                }
            else:
                # Parse key-value pairs within the current storage
                if current_storage:
                    parts = line.split(None, 1)
                    key = parts[0].strip()
                    sanitized_key = sanitize_key(key)
                    if len(parts) > 1:
                        # Regular key-value pair
                        current_storage[sanitized_key] = parts[1].strip()
                    else:
                        # Key with no value, set it to True
                        current_storage[sanitized_key] = True

    # Append the last storage section to the list if any
    if current_storage:
        storage_list.append(current_storage)

    # Update the cache
    _cached_storage_data = storage_list
    _cached_mtime = current_mtime

    return storage_list

def get_storage_size(storage):
    try:
        if storage["type"] in ["dir", "nfs", "cephfs", "zfspool"]:
            if storage["type"] == "zfspool":
                path = storage["mountpoint"]
            else:
                path = storage["path"]
            # Get filesystem statistics
            stats = os.statvfs(path)
            # Calculate total size and free space in bytes
            # TODO: find an alternative way to calculate total_size for ZFS
            total_size = stats.f_frsize * stats.f_blocks
            free_space = stats.f_frsize * stats.f_bavail
            return {
                "total": total_size,
                "free": free_space
            }

        # TODO: handle lvmthin
        # could parse /etc/lvm/backup/<vg-name> to collect this data
        # TODO: handle rbd

    except Exception as e:
        logging.warn(f"get_storage_size: unknown error, {storage=}, error: {e}")

    # Return None if the case is not handled
    return None

def collect_storage_metrics():
    logging.debug("collect_storage_metrics() called")
    gauge_dict = {}
    info_dict = {}
    prefix = cli_args.metrics_prefix
    for name, description, labels in gauge_settings:
        gauge_dict[name] = GaugeMetricFamily(f"{prefix}_{name}", description, labels=labels)

    for name, description in info_settings:
        info_dict[name] = InfoMetricFamily(f"{prefix}_{name}", description)

    storage_pools = parse_storage_cfg()
    for storage in storage_pools:
        # Convert any non-string values to strings for InfoMetricFamily
        storage_info = {}
        for key, value in storage.items():
            storage_info[key] = str(value) if not isinstance(value, str) else value

        info_dict["node_storage"].add_metric([], storage_info)
        size = get_storage_size(storage)
        if size != None:
            gauge_dict["node_storage_size"].add_metric([storage["name"], storage["type"]], size["total"])
            gauge_dict["node_storage_free"].add_metric([storage["name"], storage["type"]], size["free"])

    for v in info_dict.values():
        yield v
    for v in gauge_dict.values():
        yield v

    logging.debug("collect_storage_metrics() return")
