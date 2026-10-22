#!/bin/sh
# Babysitter for tv-watch: keeps the payload HTTP server and tv_watch.py alive.
# tv_watch.py touches state/beat every tick; if it goes stale (>30s) the watcher
# is killed and restarted (recovers from any wedge). Run as the service main
# process by tv-watch.service, which supplies TV_HOST / PAYLOAD_PORT via its .env
# (inherited by the children spawned here).
SVC="$(cd "$(dirname "$0")/.." && pwd)"   # services/tv-watch
PORT="${PAYLOAD_PORT:-8099}"
STATE="$SVC/state"
mkdir -p "$STATE"
while true; do
  pgrep -f "[h]ttp.server $PORT" > /dev/null || \
    (nohup python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$SVC/payloads" >> "$STATE/http.log" 2>&1 &)
  AGE=999
  [ -f "$STATE/beat" ] && AGE=$(( $(date +%s) - $(stat -c %Y "$STATE/beat") ))
  if [ "$AGE" -gt 30 ]; then
    pgrep -f "[t]v_watch.py" | xargs -r kill 2>/dev/null
    sleep 1
    nohup python3 "$SVC/src/tv_watch.py" >> "$STATE/watch_out.log" 2>&1 &
    echo "$(date) supervisor: restarted watcher (beat age ${AGE}s)" >> "$STATE/tv_watch.log"
  fi
  sleep 10
done
