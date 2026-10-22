# tv-watch

Serves the Vizio TV's web client and auto-launches it when the TV powers on —
without ever fighting the user over an HDMI/PS5 input. blacklodge only.

## Parts
- `bin/run_watcher.sh` — the babysitter (the service's main process). Keeps a
  `python -m http.server $PAYLOAD_PORT` serving `payloads/` alive, and restarts
  `src/tv_watch.py` if its `state/beat` heartbeat goes stale (>30s).
- `src/tv_watch.py` — polls the TV's SmartCast API (`:7345`, ~4s) and launches the
  Jellyfin client (URL from `jf_url.txt`) only on a genuine power-on (API
  unreachable ~2.5 min then back) or a return to SmartCast; never over an active
  HDMI/TV input; quiet after the user exits mid-session or after 5 failed tries.
- `payloads/` — the TV client: `jellyfin.html` (home + poster browser + custom
  player), `sports.html` (→ sports-api), `hls.light.min.js` (hls.js 0.14.1, the
  last build that behaves in the TV's Chromium 72). Both pages derive their API
  base from `location.hostname`, so no server address is baked in.
- `launcher/` — vega-side, run by hand from the deploy runbook (needs node):
  `tv_pwn.js` (pair with / launch the app on the TV's `:7345` API) and
  `alert_user.sh` (audible ping before touching the screen).

## Config & secrets (all gitignored; `*.example` committed)
- `.env`: `TV_HOST` (the TV's LAN IP), `PAYLOAD_PORT` (default 8099).
- `jf_url.txt`: the entry URL with Jellyfin auth params (`srv`/`u`/`k`) — see
  `jf_url.example.txt`. Bump its `&v=N` when `jellyfin.html` changes (cache-bust).
- `tv_token.json`: the TV pairing token — see `tv_token.example.json`
  (re-pair with `launcher/tv_pwn.js pair` if lost).

## Deploy (blacklodge)
```sh
cp services/tv-watch/.env.example       services/tv-watch/.env         # set TV_HOST
cp services/tv-watch/jf_url.example.txt services/tv-watch/jf_url.txt   # real auth
cp services/tv-watch/tv_token.example.json services/tv-watch/tv_token.json
deploy/install.sh tv-watch         # installs + enables tv-watch.service (Restart=always)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8099/jellyfin.html
```
This replaces the old `@reboot` crontab babysitter — remove that line so the
watcher isn't started twice. Runtime state (`beat`, `*.log`) lives in `state/`.

## Deploy runbook (push a client change, from vega)
```
1. edit services/tv-watch/payloads/*.html   (in the checkout)
2. JS check: awk '/<script>/{f=1;next} /<\/script>/{f=0;next} f' FILE > /tmp/c.js && node --check /tmp/c.js
3. commit + push; pull on blacklodge
4. jellyfin.html changed? bump &v=N in services/tv-watch/jf_url.txt (cache-bust);
   sports.html is fetched with ?cb=Date.now() so it refreshes on next tile entry
5. bash services/tv-watch/launcher/alert_user.sh         # ping the user
6. TV_IP=<tv> node services/tv-watch/launcher/tv_pwn.js launch "$(cat services/tv-watch/jf_url.txt)" 4 3010
7. verify: grep jellyfin.html state/http.log | tail   (GET ...v=N)
```
