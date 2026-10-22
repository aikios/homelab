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
