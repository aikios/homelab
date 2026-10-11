# stacks/homeassistant

Home Assistant, run as a Docker container. Config and state live entirely
under `./config` (bind-mounted into the container at `/config`) — nothing
persistent lives inside the container image itself. That means the whole
service is defined by two things: this directory's `docker-compose.yml`
and the `./config` folder next to it (gitignored — it holds state/secrets).

## Why this setup

Chosen for portability, since this is planned to move from the current
Ubuntu box to a Mac mini later:

- **Docker Compose, not native install** — `docker compose up -d` works
  identically on Linux and macOS (Docker Desktop). No apt packages, no
  Python venvs, no systemd units tied to this specific OS.
- **All state in a bind mount** — `./config` is a plain directory HA
  writes to (YAML configs, the `home-assistant_v2.db` SQLite database,
  logs, `.storage/` for UI-managed entities). Nothing is stored in a
  named Docker volume or inside the container, so moving the service is
  just copying a folder.
- **No host-specific packages baked in** — the image
  (`ghcr.io/home-assistant/home-assistant:stable`) is pulled fresh on
  whatever host runs it; multi-arch, so it works on Apple Silicon too.

## Deployment notes

- `network_mode: host` — required so Home Assistant can discover devices
  on the LAN via mDNS/SSDP/UPnP (smart plugs, Chromecasts, etc.). This
  is the standard recommendation from Home Assistant's own docs for
  container installs.
- `privileged: true` — grants access to host devices (e.g. a USB
  Zigbee/Z-Wave dongle or the onboard Bluetooth adapter), should you add
  one later. Not required for WiFi-only smart plugs; safe to remove if
  you never plan to attach USB radios (drop it and see if anything you
  use breaks).

## Day-to-day

```
cd stacks/homeassistant
docker compose up -d       # start / recreate
docker compose down        # stop (config is preserved)
docker compose logs -f     # tail logs
docker compose pull && docker compose up -d   # update to latest image
```

UI: `http://<host>:8123`.

## Backup

Everything that matters is in `./config`. To back it up:

```
tar czf homeassistant-config-$(date +%Y%m%d).tar.gz -C stacks/homeassistant config
```

Do this before any migration or major HA version upgrade.

## Migrating to a new host (e.g. the Mac mini)

1. On the new host, install Docker Desktop (macOS) or Docker Engine (Linux).
2. Stop the service on the old host: `docker compose down`.
3. Copy this directory (including `./config`) to the new host.
4. On the new host: `docker compose up -d`.
5. Point DNS/bookmarks at the new host's IP on port 8123.

`network_mode: host` behaves slightly differently on Docker Desktop for
Mac than on native Linux — recent Docker Desktop versions (4.29+) support
it, but if device discovery doesn't work after migrating, that's the
first thing to check (`docker compose logs` for mDNS/zeroconf errors).
