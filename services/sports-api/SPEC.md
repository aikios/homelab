# sports-api

Clean-stream extractor + HLS proxy behind the TV's "Sports" tile. Runs on
blacklodge (`:8100`, primary) and vega (`:8098`, fallback).

## How it works
- `GET /schedule` — today's events scraped from SOURCE_SITE. Event paths are
  `/<sport>/<id>` where the id may be a slug (`/nfl/redzone`), not just digits.
- `GET /event?path=/nba/123` — the raw stream-source anchors for an event.
- `GET /prepare?path=…[&since=<version>&jid=<job>&wait=<s>]` — the one the TV
  calls. Starts (or reuses, 10 min if anything worked / 60 s if not) a background
  **job** that checks every source and long-polls it: the reply comes as soon as
  the job's `version` passes `since` (or, without `since`, as soon as any source
  works). Each reply is a full snapshot (`sources[].state` = pending/ok/dead,
  `checked`, `ok_count`, `done`, `version`, `jid`). `since` is only honoured for the
  same `jid`, so a client that fails over to a different instance resyncs.
- Per source: load the page in headless Chromium (playwright), run its player JS,
  intercept the real `.m3u8`/`.mp4` (ad hosts route-blocked; stop 1.5 s after the
  first hit, 16 s budget incl. page load); a static iframe/script/`atob` BFS is
  the fallback. Candidates are validated by fetching the playlist **and the head
  of a real segment** with each plausible header set (Referer origin / page /
  +Origin). Image decoys are rejected; encrypted segments are taken on trust.
- **Host priority:** per-host outcomes are kept (decayed ×0.85 per try) in
  `state/host_stats.json` (gitignored) and sources are checked best-host-first,
  so the first working stream usually lands in the first browser wave.
- **Delegation:** if `SPORTS_DELEGATE` is set (blacklodge → vega), `/prepare` is
  forwarded there after a 1.5 s `/ping`; on failure it backs off 60 s and
  resolves locally. The TV never depends on the peer.
- `GET /pxy/<sid>/<url>` — referer-spoofing HLS proxy (rewrites playlists so every
  segment/key is fetched back through the proxy with the right headers + CORS).
  Sids are random (the old ms-timestamp sids collided under parallel validation
  and swapped referers). A failed fetch retries once, then the alternate header
  sets (sticky on success). Segments disguised behind a PNG/WebP/JPEG/GIF header
  are unwrapped to the raw TS.

Measured 2026-10-25 (live NFL Sunday, one run each, 24–28 sources/event):
| config | first working stream | all sources checked |
|---|---|---|
| old code, blacklodge | ~50 s (nothing until the end; TV timed out at 45 s) | ~50 s, truncated |
| blacklodge, 8 ctx | 5.7 s / 10.6 s | 63 s |
| blacklodge, 4 ctx | 4.7 s | 86 s |
| vega, 8 ctx | 3.6 s | 39 s |
blacklodge (4 cores) is CPU-bound (load 7.9 at 8 ctx); vega (12 cores) loads
pages 3–4× faster — hence delegation, with blacklodge at 4 ctx to spare Jellyfin.

Tools (stdlib, run against any instance):
- `bench_prepare.py <api> <path>` — timeline of a progressive prepare.
- `check_streams.py <api> <path>` — pulls a real segment of every candidate
  through the proxy and reports what the bytes are (`ts@0` = playable).
- Tests: `~/.venvs/sports/bin/python3 -m unittest discover -s tests`.

## Runtime
- **Dedicated venv** `~/.venvs/sports` (python3.10 + `playwright==1.64.0`, Chromium
  headless). Deliberately outside the shared uv workspace venv — see `bootstrap.sh`.
  This service is excluded from the uv workspace in the root `pyproject.toml`.
- Config via gitignored `.env` (see `.env.example`): `SPORTS_API_PORT`,
  `SPORTS_API_HOST` (the host's LAN address, baked into the proxy URLs the TV uses),
  `SPORTS_MAX_CTX` (browser contexts, default 8), `SPORTS_DELEGATE` (peer URL).
- **Deploy order:** upgrade the delegate (vega) before the primary — an old peer
  doesn't speak the progressive `/prepare` protocol.

## Deploy
```sh
services/sports-api/bootstrap.sh                       # one-time: build the venv + chromium
cp services/sports-api/.env.example services/sports-api/.env   # set PORT + HOST
deploy/install.sh sports-api                           # install + enable sports-api.service
curl http://localhost:$SPORTS_API_PORT/schedule        # smoke test
```
The TV's `sports.html` always talks to blacklodge (`:8100`); blacklodge hands
extraction to vega when it's up. `sports.html?api=<port>` points the page at
another instance for testing.
