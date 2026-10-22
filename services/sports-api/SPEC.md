# sports-api

Clean-stream extractor + HLS proxy behind the TV's "Sports" tile. Runs on
blacklodge (`:8100`, primary) and vega (`:8098`, fallback).

## How it works
- `GET /schedule` — today's games scraped from watchsports.su.
- `GET /event?path=/nba/123` — the raw stream-source anchors for an event.
- `GET /prepare?path=/nba/123` — the one the TV calls: for every source, load the
  page in headless Chromium (playwright), execute its player JS, and intercept the
  real `.m3u8`/`.mp4` requests. Ad hosts are route-blocked so the browser never
  loads them; a static iframe/script/`atob` BFS is the fallback when the browser
  finds nothing. Each candidate is validated with a referer probe, in parallel.
- `GET /pxy/<sid>/<url>` — referer-spoofing HLS proxy (rewrites playlists so every
  segment/key is fetched back through the proxy with the right referer + CORS).

## Runtime
- **Dedicated venv** `~/.venvs/sports` (python3.10 + `playwright==1.64.0`, Chromium
  headless). Deliberately outside the shared uv workspace venv — see `bootstrap.sh`.
  This service is excluded from the uv workspace in the root `pyproject.toml`.
- Config via gitignored `.env` (see `.env.example`): `SPORTS_API_PORT`,
  `SPORTS_API_HOST` (the host's LAN address, baked into the proxy URLs the TV uses).

## Deploy
```sh
services/sports-api/bootstrap.sh                       # one-time: build the venv + chromium
cp services/sports-api/.env.example services/sports-api/.env   # set PORT + HOST
deploy/install.sh sports-api                           # install + enable sports-api.service
curl http://localhost:$SPORTS_API_PORT/schedule        # smoke test
```
vega keeps its own instance as a fallback; the TV's `sports.html` points at the
primary and can be flipped to vega if it dies.
