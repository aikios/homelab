# tv-launcher

Serves the Vizio TV's web client and opens it on the TV when the TV powers on,
without ever fighting the user over an HDMI/PS5 input. blacklodge only.
(Formerly `tv-watch`; restructured 2026-10-26.)

## Units
- `tv-launcher.service` — `src/tv_launcher/watcher.py`: polls the TV's SmartCast
  API (`:7345`, ~4s) and launches the client (URL from `jf_url.txt`) only on a
  genuine power-on (API unreachable ~2.5 min then back) or a return to SmartCast;
  never over an active HDMI/TV input; quiet after the user exits mid-session or
  after 5 failed tries. Pings systemd's watchdog every tick (`WatchdogSec=45`), so
  a wedged watcher is killed and restarted (this replaced the old shell
  babysitter and its `state/beat` file). Log: `state/watcher.log` + journal.
- `tv-launcher-pages.service` — `python -m http.server $TV_LAUNCHER_PORT` serving
  `static/`; request log in `state/http.log`.

## Files
- `static/` — the TV client: `jellyfin.html` (home + poster browser + custom
  player), `sports.html` (→ stream-resolver), `hls.light.min.js` (hls.js 0.14.1,
  the last build that behaves in the TV's Chromium 72). Both pages derive their
  API base from `location.hostname`, so no server address is baked in.
- `scripts/` — run by hand from the deploy runbook (needs node for the .js):
  `tv_launch.js` (pair with / launch an app on the TV's `:7345` API) and
  `alert_user.sh` (audible ping before touching the screen).

## Config & secrets (all gitignored; `*.example` committed)
- `.env`: `TV_LAUNCHER_TV_HOST` (the TV's LAN IP), `TV_LAUNCHER_PORT` (default 8099).
- `jf_url.txt`: the entry URL with Jellyfin auth params (`srv`/`u`/`k`) — see
  `jf_url.example.txt`. Bump its `&v=N` when `jellyfin.html` changes (cache-bust).
- `tv_token.json`: the TV pairing token — see `tv_token.example.json`
  (re-pair with `scripts/tv_launch.js pair` if lost).

## Deploy (blacklodge)
```sh
cp services/tv-launcher/.env.example          services/tv-launcher/.env           # set TV host
cp services/tv-launcher/jf_url.example.txt    services/tv-launcher/jf_url.txt     # real auth
cp services/tv-launcher/tv_token.example.json services/tv-launcher/tv_token.json
deploy/install.sh tv-launcher      # installs + enables both units
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8099/jellyfin.html
uv run --no-sync python -m unittest discover -s services/tv-launcher/tests
```

## Client-deploy runbook (push a page change)
```
1. edit services/tv-launcher/static/*.html   (in the checkout)
2. JS check: awk '/<script>/{f=1;next} /<\/script>/{f=0;next} f' FILE > c.js && node --check c.js
3. commit + push; pull on blacklodge
4. jellyfin.html changed? bump &v=N in services/tv-launcher/jf_url.txt (cache-bust);
   sports.html is fetched with ?cb=Date.now() so it refreshes on next tile entry
5. bash services/tv-launcher/scripts/alert_user.sh        # ping the user
6. TV_IP=<tv> node services/tv-launcher/scripts/tv_launch.js launch "$(cat services/tv-launcher/jf_url.txt)" 4 3010
7. verify: grep jellyfin.html services/tv-launcher/state/http.log | tail   (GET ...v=N)
```
