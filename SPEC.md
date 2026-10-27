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
├── services/               # owner-built Python daemons
│   ├── rain-alert/         #   workspace members: src/, systemd/*, SPEC.md
│   ├── topic-watcher/
│   ├── mediabot/
│   ├── media-transcoder/   #   TV stack: workspace member (merged reencode-worker/-submit)
│   ├── stream-resolver/    #   TV stack: workspace member (was sports-api)
│   └── tv-launcher/        #   TV stack: workspace member (was tv-watch)
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

## TV stack (imported 2026-10-22, with the user)

A self-hosted streaming stack for a Vizio M55-D0 (Conjure webview / Chromium 72).
Everything the TV needs at runtime runs on **blacklodge**; **vega** is the GPU
re-encode worker + the stream-resolver extraction peer (blacklodge delegates `/prepare` to it,
falling back locally), and the TV keeps working with vega off.
This was the first **multi-host** import and the repo is now the single source of
truth for both hosts (each has its own `git` checkout; services run from it).

Flow: the TV loads `tv-launcher`'s client (`jellyfin.html`) from `:8099`; the Sports
tile calls `stream-resolver`, which drives headless Chromium to extract clean `.m3u8`
streams and proxies them with referer-spoofing; media the TV can't direct-play is
queued on blacklodge and re-encoded to h264 on vega by `media-transcoder`.

| Service | Host(s) | Unit | Runtime |
|---|---|---|---|
| stream-resolver | blacklodge :8100, vega :8098 | `stream-resolver.service` | workspace venv (playwright 1.64 + chromium via `scripts/setup.sh`) |
| tv-launcher | blacklodge | `tv-launcher.service` (watcher) + `tv-launcher-pages.service` (`:8099`) | workspace venv; systemd watchdog replaced the shell babysitter |
| media-transcoder | blacklodge (submit, notify), vega (worker) | `media-transcoder-{submit,notify,worker}` | workspace venv + ffmpeg (NVENC/VAAPI) on vega |

Conventions the TV stack breaks from the rest (on purpose):
- **No server address in the tree** — the payloads derive their API base from
  `location.hostname`; hosts/ports/keys come from each service's gitignored `.env`.
- **Extra gitignored secrets:** `tv-launcher/jf_url.txt` (Jellyfin auth params) and
  `tv-launcher/tv_token.json` (TV pairing token); `*.example` templates are committed.

Per-service SPEC.md has the details; `tv-launcher/SPEC.md` carries the client-deploy
runbook (edit page → push → pull → cache-bust → `tv_launch.js launch`).

## Restructure plan (2026-10-26, agreed with the user; not started)
Goal: keep `services/` **flat**, with one naming scheme where every name reads as
a service and says what it does.

### Naming scheme
Every service is `<what it works on>-<what it does>`, kebab-case, and the second
part is always an agent noun for the job (`-notifier`, `-resolver`, …), never the
implementation shape (no `-api`, `-bot`, `-app`).

| now                               | new                | what it does                                                        |
|-----------------------------------|--------------------|---------------------------------------------------------------------|
| rain-alert                        | `rain-notifier`    | morning forecast check; DMs to bring the patio chair in             |
| topic-watcher                     | `news-notifier`    | daily news digest for chat-managed watched topics                   |
| mediabot                          | `media-concierge`  | Discord assistant: find, download, organize, subtitle media         |
| sports-api                        | `stream-resolver`  | resolves working live sports streams and relays them to the TV      |
| tv-watch                          | `tv-launcher`      | serves the TV client pages and opens them when the TV turns on      |
| reencode-worker + reencode-submit | `media-transcoder` | re-encodes library files the TV can't play, on the GPU host         |

That one name is derived everywhere, no exceptions:
- dir `services/<name>/`, distribution `<name>`, package `<name_snake>`
- systemd: `<name>.service`, or `<name>-<cmd>.{service,timer,path}` when a service
  has several units. **Every unit starts with its service name.**
- env: every service-specific setting is `<NAME_SNAKE_UPPER>_*` (incl. all of
  mediabot's tunables). Third-party creds/URLs keep their vendor names
  (`JELLYFIN_URL`, `QBIT_USER`, `DISCORD_BOT_TOKEN`, …).
- runtime state in `services/<name>/state/` (gitignored).

Every service has the same layout:
```
services/<name>/
  pyproject.toml        workspace member; [project.scripts] <name>[-<cmd>]
  src/<name_snake>/     __main__.py + modules
  systemd/              <name>[-<cmd>].{service,timer,path}
  tests/
  scripts/              dev/ops tools (bench, checks, one-offs)
  static/               files served to a device (tv-launcher only)
  .env.example  SPEC.md
```
All services become uv workspace members: the root `exclude` list, the dedicated
`~/.venvs/sports`, `requirements.txt` and `bootstrap.sh` go away (playwright joins
the shared venv; install.sh runs `playwright install chromium` for
stream-resolver). Validate on both hosts before relying on it.

### Units after the rename
- rain-notifier.{service,timer}
- news-notifier.{service,timer}, news-notifier-listen.service
- media-concierge.service, media-concierge-trash.{service,timer}
- stream-resolver.service (both hosts)
- tv-launcher.service
- media-transcoder-worker.service (GPU host), media-transcoder-submit.{service,timer},
  media-transcoder-notify.{service,path}

Also: `tv_pwn.js` -> `tv_launch.js`; `payloads/` -> `static/`, `bin/`+`launcher/` ->
`scripts/`; `farm_core`/`farm_submit`/`farm_notify`/`reencode_worker` -> modules
`core`/`submit`/`notify`/`worker` of one `media_transcoder` package (the shared
queue code stops being split across two dirs); mediabot drops its duplicate
README.md (SPEC.md is the doc); `services/.gitkeep` goes.
Env renames: `SPORTS_*` -> `STREAM_RESOLVER_*`, `FARM_*` -> `MEDIA_TRANSCODER_*`,
`TV_HOST`/`PAYLOAD_PORT` -> `TV_LAUNCHER_*`, mediabot tunables (`SUBS_*`, `QBMON_*`,
`COST_*`, ...) -> `MEDIA_CONCIERGE_*`, topic-watcher -> `NEWS_NOTIFIER_*`,
`RAIN_ALERT_*` -> `RAIN_NOTIFIER_*`.
External paths keep working: the TV URL (`:8099/jellyfin.html`) and the queue dir
`~/reencode_farm` are unchanged (renaming the queue dir would need a coordinated
two-host move for no user-visible gain).

### Migration (one service per branch, each verified live before the next)
0. `install.sh` learns renames: a `deploy/renames` map (old unit -> new) so it
   stops/disables/removes the old unit when installing the new one; per-host
   `.env` key renames via a small script (keys only, values never printed).
1. media-transcoder: GPU-host worker first, then submit/notify; synthetic job.
2. stream-resolver: package + scripts/; vega first (blacklodge delegates to it).
3. tv-launcher: payload server + watcher; `curl :8099/jellyfin.html`, on-TV check.
4. news-notifier: listener + timer; test a chat command and a run.
5. rain-notifier: timer; manual run.
6. media-concierge: last (most live); bot reconnects, one agent turn.
7. Docs + cleanup: root SPEC/README/CLAUDE.md, `hosts.example.yml`, memory; retire
   `~/services/{rain-alert,topic-watcher,mediabot}`, `~/vizio_watch` (copy
   `jf_url.txt` out first: it was the only backup on 2026-10-26) and vega's
   `~/vizio_recon/scratchpad`.

## Open / to decide later
- **Rotate the Jellyfin API key** — it sat hardcoded in the pre-import reencode
  scripts (plaintext on two hosts); treat as exposed now that history is clean.
- **sports-api** carries pre-existing ruff/ty debt (defensive-scraper blind
  excepts, %-format, a stdlib handler override) — left as-is like mediabot; its
  own cleanup pass.
- **Retire the pre-import runtime dirs** (`~/vizio_watch` on blacklodge, vega's
  `~/vizio_recon/scratchpad`) once fully confident — kept as fallbacks for now.
- **On-TV re-test** of the `sports.html` `location.hostname` change when the TV is
  next on (verified server-side; it auto-refreshes via its cache-buster).
- vega now has a full workspace venv from `install.sh`'s `uv sync --all-packages`
  even though it only runs stdlib services — harmless, revisit if it bloats.
- Clean up mediabot's pre-existing ruff (66) / ty (16) findings — noqa-code drift
  vs current ruff + intentional patterns (SLF001, defensive BLE001/S110). Separate
  from the migration; touches live code.
- Smoke-test a real mediabot agent turn (owner DM) after the anthropic 1.4→1.13
  bump — import succeeds, but that doesn't exercise `tool_runner`.
- Secret delivery beyond `.env` per host (sops/age?) if hosts multiply.
- Whether to add Dockerfiles for the self-contained Python services (rain-alert,
  topic-watcher) for image-based portability — not needed now.
- Extracting a generic, host-free public "service template" repo for showcasing.
