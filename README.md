# PVEmon

PVEmon is a Prometheus exporter for Proxmox VE (PVE). Unlike prometheus-pve-exporter, this collects metrics locally from the host system instead of using the PVE API. This project is packaged using Nix.

## Features

- Collects and exports metrics about KVM virtual machines running on the local host.
- Metrics include CPU usage, memory usage, IO statistics, and more.
- Exports metrics at a `/metrics` HTTP endpoint for scraping by a Prometheus server.
- The Nix flake provides a `deb` build output for creating an installable package for PVE servers.
- Currently, the tool supports monitoring certain aspects of VMs. Support for LXC and additional VM metrics are planned for future releases.

## Installation

This project is packaged using Nix. To build a deb for your PVE servers, run:

```bash
nix build github:illustris/pvemon#deb
```
