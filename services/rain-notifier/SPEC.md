# rain-notifier

Once each morning, check today's rain chance at the home location and DM the
owner on Discord to bring the patio chair inside if rain is likely. Silent
otherwise. This is the **pilot migration** (SPEC.md step 2): it proves the whole
pipeline — `homelab-core` extraction, workspace sync, systemd-from-repo deploy.

## Behavior
- One Open-Meteo `daily` forecast request (free, no API key, stdlib `urllib`).
- Alerts when today's `precipitation_probability_max` ≥ **40%**.
- Message includes the probability and expected precipitation (inches).
- No LLM calls; canned message.

## What it reuses from `homelab-core`
- `homelab_core.ha.home_location()` — home coordinates from the local Home
  Assistant `core.config` (root-owned, via `sudo -n`). Single source of truth
  for location. If that read fails, the only fallback is a `RAIN_NOTIFIER_LAT` /
  `RAIN_NOTIFIER_LON` env override; with neither, the run skips (no location is
  hardcoded — public repo).
- `homelab_core.notify.send_discord_dm()` — opens the owner DM channel and posts,
  with the bot token passed to `curl` via `-K -` (stdin, never argv) and an
  explicit `User-Agent` (Discord's Cloudflare blocks bare clients).
- `homelab_core.config.shared_env()` — reads `DISCORD_BOT_TOKEN` /
  `OWNER_DISCORD_ID` from the shared `.env` named by `HOMELAB_SHARED_ENV`
  (set in the systemd unit to mediabot's `.env`).

## Run
```sh
# --all-packages keeps every workspace member installed in the shared venv.
uv run --all-packages rain-notifier --dry-run   # forecast + decision, send nothing
uv run --all-packages rain-notifier --force     # send regardless (test delivery)
uv run --all-packages rain-notifier             # alert only if rain is likely
```

## Deploy
```sh
deploy/install.sh rain-notifier     # uv sync + install & enable the timer
```
The `rain-notifier.timer` fires daily at 08:00 local (America/Chicago) with
`Persistent=true` (catches up a missed run after downtime). The `.service` is a
`static` oneshot driven by the timer. Runtime log: `state/rain_notifier.log`
(gitignored).
