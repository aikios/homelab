# reencode-submit

Media-host side of the GPU re-encode farm (runs on blacklodge). A CLI — no
systemd unit. Finds library items the TV can't direct-play and drops jobs in the
queue for the **reencode-worker** (vega) to claim and process.

## Usage
```sh
python3 services/reencode-submit/src/farm_submit.py            # show queue/claimed/done/failed
python3 services/reencode-submit/src/farm_submit.py --scan     # queue every non-direct-playable item
python3 services/reencode-submit/src/farm_submit.py --list     # same as no-arg
python3 services/reencode-submit/src/farm_submit.py <file> ...  # submit explicit paths
```
`--scan` is idempotent: anything already queued/claimed/done is skipped; failed
jobs are retried. Jobs land in `~/reencode_farm/queue/` (`FARM_QUEUE_DIR`).

## Config (gitignored `.env`, see `.env.example`)
`JELLYFIN_URL` (local, default `http://localhost:8096`) + `JELLYFIN_API_KEY`.
The CLI auto-loads its sibling `.env`, so no need to export vars by hand.

## Deploy
```sh
cp services/reencode-submit/.env.example services/reencode-submit/.env   # fill in
deploy/install.sh reencode-submit     # no units — just makes the CLI present in the checkout
```
Stdlib only. The queue dirs (`queue/claimed/done/failed`) are runtime state,
created on first run, and live outside the repo under `~/reencode_farm`.

## Auto-scan (wired into the download workflow)
`reencode-submit.timer` runs `--scan` every 5 min (`OnUnitActiveSec=5min`), so
media imported by Radarr/Sonarr is queued for the GPU farm within ~5 min with no
manual step. `--scan` is idempotent (skips queued/claimed/done), so the poll is
cheap. Event-driven *arr hooks were avoided because the *arr apps run in Docker
and can't reach the host queue without bind-mount plumbing; the 5-min poll is
near-immediate for a background batch op. Deploy installs + enables the timer.

## Discord notifications (`src/farm_notify.py`)
The owner gets a DM (via homelab-core, shared bot token in `secrets/shared.env`):
- **Queued**: one batched DM per `--scan` / explicit submit, listing each file
  with its container, video/audio codec (from Jellyfin) and size. Jobs now carry
  `source_bytes` + `why`.
- **Re-encoded and replaced / failed / skipped**: the worker writes a `result`
  block into the job json (`status`, `detail`, `mode`, `out_bytes`, `secs`, `subs`,
  `replaced`, `error`, `worker`, `finished`) and only then `mv`-s it into `done/`
  or `failed/`. `reencode-notify.path` (PathChanged on both dirs) fires
  `reencode-notify.service` the moment that happens; `--scan` also runs the
  notifier as a backstop for a missed trigger.
- Already-reported ids live in `~/reencode_farm/notified.json` (the first run seeds
  it with the existing backlog so nothing old is replayed). An id is only marked
  after Discord accepts the DM, so an outage retries on the next fire; a flock
  serialises the path unit and the backstop. Sending is best-effort and never
  breaks submitting or filing.
- The path unit hard-codes the default queue dir (`/home/<user>/reencode_farm`);
  change it there too if you override `FARM_QUEUE_DIR`.
- Tests: `python3 -m unittest discover -s services/reencode-submit/tests`.
