# stream-resolver

Resolves working live sports streams and relays them to the TV: clean-stream
extractor + HLS proxy behind the TV's "Sports" tile (formerly `sports-api`).
The source site is a secret: it lives only in the gitignored `.env`. Runs on
blacklodge (`:8100`, primary) and vega (`:8098`, fallback).

## How it works
- `GET /schedule` — today's events scraped from the source site
  (`STREAM_RESOLVER_SOURCE_URL`, kept only in the gitignored `.env`, never in the repo). Event paths are
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
- **Delegation:** if `STREAM_RESOLVER_DELEGATE` is set (blacklodge → vega), `/prepare` is
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

Tools (`scripts/`, stdlib, run against any instance):
- `bench_prepare.py <api> <path>` — timeline of a progressive prepare.
- `check_streams.py <api> <path>` — pulls a real segment of every candidate
  through the proxy and reports what the bytes are (`ts@0` = playable).
- Tests: `uv run --no-sync python -m unittest discover -s services/stream-resolver/tests`.

## Runtime
- Workspace member: `playwright==1.64.0` (pinned) lives in the shared venv;
  `scripts/setup.sh` (run by `install.sh`) fetches the matching headless Chromium
  and installs its system libraries only if it can't launch. (Until 2026-10-26 it
  had a dedicated `~/.venvs/sports`.)
- Config via gitignored `.env` (see `.env.example`): `STREAM_RESOLVER_PORT`,
  `STREAM_RESOLVER_HOST` (the host's LAN address, baked into the proxy URLs the TV
  uses), `STREAM_RESOLVER_MAX_CTX` (browser contexts, default 8),
  `STREAM_RESOLVER_DELEGATE` (peer URL), `STREAM_RESOLVER_SOURCE_URL`.
- **Deploy order:** upgrade the delegate (vega) before the primary — an old peer
  doesn't speak the progressive `/prepare` protocol.

## Deploy
```sh
cp services/stream-resolver/.env.example services/stream-resolver/.env   # set PORT, HOST, SOURCE_URL
deploy/install.sh stream-resolver        # sync, chromium setup, enable stream-resolver.service
curl http://localhost:$STREAM_RESOLVER_PORT/schedule                       # smoke test
```
The TV's `sports.html` always talks to blacklodge (`:8100`); blacklodge hands
extraction to vega when it's up. `sports.html?api=<port>` points the page at
another instance for testing.
