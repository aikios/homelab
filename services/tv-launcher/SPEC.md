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

## UI captures in the TV's browser (`scripts/ui_capture.py`)
The TV runs Chromium 72, so modern browsers hide real layout bugs (e.g. flex
`gap` does not exist there: it silently collapsed the player's control row).
`ui_capture.py` drives a Chromium 72 snapshot headless at 3840x2160 (the width
the TV reports, so the pages take their real `zoom` path) with remote keys, and
writes a 1920x1080 PNG per screen plus `metrics.json` (clipped text, text outside
the 5% safe area, contrast under 4.5:1).

Setup on vega (one-off; nothing below is committed):
- Chromium 72.0.3617.0 = snapshot r609904 (the build Puppeteer 1.11.0 pinned):
  `~/ui-harness/chromium/r609904/chrome-linux/chrome` (from the
  chromium-browser-snapshots bucket; `--version` verified).
- `~/ui-harness/jf.env` (mode 600): `JF_SRV`, `JF_U`, `JF_K` from `jf_url.txt`,
  `SR=http://127.0.0.1:8098`.
- Copy `scripts/` + `static/` into a work dir and run:
  ```sh
  uv run --with websocket-client python scripts/ui_capture.py \
    --chrome ~/ui-harness/chromium/r609904/chrome-linux/chrome \
    --env ~/ui-harness/jf.env --out ~/ui-harness/shots/after
  ```
API responses are recorded on the first run and replayed after
(`~/ui-harness/cache`), so before/after captures see identical data. Snapshot
Chromium can't decode h264: player screens put the item's (or series') backdrop
behind the controls as a stand-in frame, and the sports autoplay toast
("All sources failed") is a harness artifact.

2026-10-27 pass (from these captures): player title was the body's navy on video
(now white on a top scrim, series/season/episode on its own line); control row
spacing via margins instead of flex `gap`; sports rows are a fixed two-line layout
(title, detail ellipsized under it) with 14px spacing, one 32px gutter, no
duplicate "Live" status, `·` separators, league tabs upper-cased, and no
"BACK returns to schedule" hint.

## Playback progress (resume)
The player writes the position to the user's Jellyfin UserData
(`POST /UserItems/{id}/UserData?userId=`, body `PlaybackPositionTicks` / `Played`;
works with the client's API key on 10.11.6) every ~10 s while playing, on pause,
on close and on app exit (`keepalive` fetch). Stock thresholds: < 5 % is saved as
0 (not resumable), >= 90 % sets `Played` and clears the position. Nothing is
written before the `playing` event, so a failed load never clobbers a resume
point. Opening an item with a saved position shows a Resume / Start over chooser
(Resume focused). Lists show a progress bar (episode thumbs, movie posters) and
"Watched" on finished episodes (`enableUserData=true` on the list queries).
