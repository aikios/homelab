# homelab — home server as code

A monorepo for everything running on a home server (and future
hosts): owner-built Python services formalized as reusable daemons, plus the
third-party app stacks captured as config.

## Goals
- One repo to reproduce the server on new hardware.
- Owner services become reusable packages (shared code instead of copy-paste).
- Clear, uniform deploy: `git clone` + `deploy/install.sh <service>` on any
  Linux host with systemd + uv.

## Decisions (2026-10-11, with the user)
- **Scope:** everything — owner Python services + third-party stacks as config.
- **Deploy model (hybrid):** owner Python services run on **systemd + uv** (no
  Docker — they need host integration: the shared `.env`, root-owned Home
  Assistant config reads, the 2s Discord DM loop, local timers). Third-party
  apps (Jellyfin, *arr, Home Assistant, Matter, flaresolverr, jellyfin-mcp,
  openclaw) stay on their **official images**, versioned here as
  `docker-compose.yml` + `.env.example`.
- **Repo:** public GitHub (`github.com/<your-account>/homelab`), **hardened** —
  secrets stripped, gitleaks + push-protection, only `*.example` committed, real
  host inventory gitignored. (A public infra repo exposes the software inventory
  and network topology; we keep real IPs/IDs/tokens out of the tracked tree.)
- **Dev model:** develop locally, push to GitHub, deploy to hosts over SSH
  (the host already has passwordless SSH + sudo).

## Layout
```
homelab/
├── pyproject.toml          # uv workspace root (one lockfile for all services)
├── libs/core/              # homelab-core: shared code
│   └── src/homelab_core/   #   config (env/secrets), notify (Discord), ha (location)
├── services/               # owner-built Python daemons (workspace members)
│   ├── rain-alert/         #   each: src/, systemd/*.service|*.timer, SPEC.md
│   ├── topic-watcher/
│   └── mediabot/
├── stacks/                 # third-party apps as config-as-code
│   └── <app>/docker-compose.yml + .env.example
├── deploy/
│   ├── install.sh          # per-host: uv sync → install+enable units
│   └── hosts/hosts.example.yml   # which services/stacks run on which host
└── .pre-commit-config.yaml # gitleaks
```

## Current server state (inventoried 2026-10-11, Ubuntu 22.04)
Owner Python services (git + uv + systemd):
- **mediabot** — Discord bot (anthropic Tool Runner), `mediabot.service` (resident).
- **topic-watcher** — `topic-watcher-listen.service` (2s DM loop) + `topic-watcher.timer`
  (daily Google News) + oneshot `topic-watcher.service`.
- **rain-alert** — `rain-alert.timer` (08:00) + oneshot `rain-alert.service`.

Third-party (docker-compose): arr-stack (radarr/sonarr/prowlarr/qbittorrent/
jellyseerr), jellyfin-mcp, flaresolverr, matter-server, homeassistant, openclaw.

Shared patterns to fold into `libs/core`:
- Discord delivery: one bot token (historically mediabot's `.env`) reused by all;
  curl `-K -` (token via stdin, never argv) + explicit `User-Agent` (Discord's
  Cloudflare returns 403 `error code: 1010` to bare clients).
- Home Assistant home coordinates (root-owned `core.config`, needs sudo) as the
  single source of truth for location.
- env/secret loading from a shared, uncommitted `.env`.

Host notes: timezone America/Chicago (systemd OnCalendar = local Central).
Python 3.10.12. `uv` at `~/.local/bin/uv`, NOT on PATH over non-interactive ssh.
Passwordless `sudo -n`.

## Security (public repo)
- Never commit: `.env`/secrets, runtime state, real host inventory (IPs/hostnames),
  owner Discord IDs, tokens. Only `*.example` files.
- gitleaks pre-commit hook; enable GitHub secret-scanning + push-protection.
- Discord token always via curl `-K -` (stdin), never argv/env visible in `ps`.
- Render user-provided / external text safely; validate inputs.

## Migration plan (validate small, then scale)
1. [x] Scaffold workspace + `libs/core` interfaces + `deploy/install.sh`.
2. [x] **Pilot: rain-alert.** Ported in; coords from `core.ha`, delivery via
       `core.notify`; runs from the repo on the server, verified live
       (`--dry-run`, `--force`, oneshot under systemd). Proved the pipeline.
3. [x] topic-watcher (resident 2s `--listen` loop + daily news timer). Ported in;
       `core.notify` grew channel-level primitives (open/send/fetch) to cover the
       listener. Deploy model settled: `uv sync --all-packages` + units run with
       `uv run --no-sync` (members coexist in one venv, no cross-eviction).
4. [x] mediabot (anthropic Tool Runner, resident). Faithful lift-and-shift into
       services/mediabot; shared creds moved to host-level `secrets/shared.env`,
       per-service keys in its own `.env`; runs from the repo on the server,
       verified live (Gateway connect as owner bot). Pre-existing ruff/ty debt
       left as-is (not introduced here).
5. [x] stacks/: exported all six compose projects (arr-stack, flaresolverr,
       homeassistant, jellyfin-mcp, matter-server, openclaw) with scrubbed
       `.env.example`s. A scoped `stacks/.gitignore` whitelists only compose +
       env-examples + docs, so no bind-mounted state/secret can be committed.
6. [x] hosts inventory (`deploy/hosts/hosts.local.yml`, gitignored) + one-command
       `deploy/host-up.sh [host]`: reads the inventory, runs `install.sh` for each
       assigned owner service and `docker compose up -d` for each assigned stack
       (skipping a stack whose `.env` isn't set yet). `_inventory.py` parses it.

## Decisions (later additions)
- **Discord UI layer (`core.ui`).** `Messenger` is the small *outbound* contract
  all services share (`send_dm` / `send`); `core.ui.Discord` is the REST-over-curl
  backend (stdlib-only, lazy-cached DM channel) that the notifiers program against.
  Inbound stays transport-specific (REST polls via `Discord.fetch`; a Gateway bot
  gets events), so it's out of the contract. mediabot's discord.py Gateway bot is
  the *other* backend of the same conceptual layer — intentionally not rewritten
  (its event loop isn't expressible as `Messenger`); it can present `Messenger` via
  a small adapter in its own package if ever wanted.

## Open / to decide later
- Clean up mediabot's pre-existing ruff (66) / ty (16) findings — noqa-code drift
  vs current ruff + intentional patterns (SLF001, defensive BLE001/S110). Separate
  from the migration; touches live code.
- Smoke-test a real mediabot agent turn (owner DM) after the anthropic 1.4→1.13
  bump — import succeeds, but that doesn't exercise `tool_runner`.
- Secret delivery beyond `.env` per host (sops/age?) if hosts multiply.
- Whether to add Dockerfiles for the self-contained Python services (rain-alert,
  topic-watcher) for image-based portability — not needed now.
- Extracting a generic, host-free public "service template" repo for showcasing.
