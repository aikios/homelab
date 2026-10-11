#!/usr/bin/env python3
"""Read the host inventory and print a host's services/stacks/address.

Used by deploy/host-up.sh. The inventory is deploy/hosts/hosts.local.yml (real,
gitignored) if present, else deploy/hosts/hosts.example.yml.

    _inventory.py <host> {services|stacks|address}

Prints space-separated services/stacks (one field), or the ssh address string.
Exits non-zero (message on stderr) if the host or field is absent.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import yaml

HOSTS_DIR = Path(__file__).resolve().parent / "hosts"


def inventory_path() -> Path:
    local = HOSTS_DIR / "hosts.local.yml"
    return local if local.exists() else HOSTS_DIR / "hosts.example.yml"


def main(argv: list[str]) -> int:
    host = argv[0] if argv else socket.gethostname()
    field = argv[1] if len(argv) > 1 else "services"

    path = inventory_path()
    data = yaml.safe_load(path.read_text()) or {}
    hosts = data.get("hosts") or {}
    entry = hosts.get(host)
    if entry is None:
        sys.stderr.write(
            f"host {host!r} not in {path} (known: {', '.join(hosts) or 'none'})\n"
        )
        return 2

    if field == "address":
        print(entry.get("address", ""))
    elif field in ("services", "stacks"):
        print(" ".join(entry.get(field) or []))
    else:
        sys.stderr.write(f"unknown field {field!r} (want services|stacks|address)\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
