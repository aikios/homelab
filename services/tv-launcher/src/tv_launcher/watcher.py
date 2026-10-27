# watcher.py -- tv-launcher: open the TV client when the TV comes online, and
# never fight the user over HDMI/TV inputs.
#
# v2 bug this fixes: any single API timeout reset the state machine, so the
# watcher thought the TV had rebooted and force-launched the client while the
# user was on an active HDMI input (e.g. mid-PS5 session). v3 changes:
#   - "TV offline" now requires ~2.5 min of SUSTAINED API failures; blips are
#     ignored and never reset any latches
#   - input activities (current = hdmiX/tv, no app MESSAGE) latch a hands-off
#     mode: no launches while an input is active; the latch clears when the
#     user returns to SmartCast (so the client comes back then) or on a real
#     TV power cycle
#   - logs only state transitions instead of spamming every 4s poll
import json
import os
import signal
import socket
import ssl
import time
import urllib.request

# Secrets (gitignored) sit in the service dir, runtime state in its state/.
# TV_LAUNCHER_TV_HOST / TV_LAUNCHER_PORT come from the env (.env via systemd).
SVC_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_DIR = os.path.join(SVC_DIR, "state")
TV = os.environ.get("TV_LAUNCHER_TV_HOST", "")
PORT = os.environ.get("TV_LAUNCHER_PORT", "8099")
LOG = os.path.join(STATE_DIR, "watcher.log")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

FAILS_FOR_OFFLINE = 15  # consecutive failed polls before we call the TV offline
AUTH = ""    # TV pairing token, read from tv_token.json by main()
JF_URL = ""  # entry URL the TV is pointed at, read from jf_url.txt by main()


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%S") + " " + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def beat():
    """Ping systemd's watchdog (WatchdogSec in the unit); no-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(b"WATCHDOG=1")
    except OSError as e:
        log("watchdog ping failed: " + repr(e)[:120])


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"https://{TV}:7345{path}", data=data, method=method)
    req.add_header("AUTH", AUTH)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=6, context=CTX) as r:
        return r.status, json.loads(r.read().decode())


def current():
    try:
        _, j = api("GET", "/app/current")
        item = j.get("ITEM") or (j.get("ITEMS") or [{}])[0]
        return item.get("VALUE") or {}
    except Exception as e:  # noqa: BLE001 — a poll failure is expected (TV off); caller treats None as offline
        log("current err: " + repr(e)[:120])
        return None


def classify(cur):
    """ours = our web client; input = hdmi/tv activity (hands off); other = SmartCast home etc."""
    msg = str((cur or {}).get("MESSAGE") or "")
    if PORT in msg and ("jellyfin" in msg.lower() or "sports" in msg.lower()):
        return "ours"
    if not msg and (cur or {}).get("APP_ID"):
        return "input"
    return "other"


def summarize(cur):
    if not cur:
        return ""
    return f"app_id={cur.get('APP_ID')} ns={cur.get('NAME_SPACE')} msg={str(cur.get('MESSAGE'))[:60]}"


def _hb(signum, frame):
    pass


def main():
    global AUTH, JF_URL
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(SVC_DIR, "tv_token.json")) as f:
        AUTH = json.load(f)["token"]
    with open(os.path.join(SVC_DIR, "jf_url.txt")) as f:
        JF_URL = f.read().strip()

    signal.signal(signal.SIGALRM, _hb)
    signal.setitimer(signal.ITIMER_REAL, 5, 5)  # hang-proof heartbeat

    up = False          # API seen recently
    session = False     # our app seen this boot cycle -> don't override user exits
    fail_n = 0
    tries = 0
    last_kind = None
    log(f"watcher v3 started; TV {TV}")
    while True:
        beat()
        cur = current()
        if cur is None:
            fail_n += 1
            if up and fail_n >= FAILS_FOR_OFFLINE:
                up = False
                session = False
                tries = 0
                log("TV offline (sustained); state reset")
            time.sleep(4)
            continue
        fail_n = 0
        if not up:
            up = True
            session = False
            tries = 0
            log("TV API online (boot)")
            time.sleep(8)  # let middleware settle
            beat()
            cur = current()
        kind = classify(cur)
        if kind != last_kind:
            log(f"state: {kind} ({summarize(cur)})")
            last_kind = kind
        if kind == "ours":
            session = True
            tries = 0
        elif kind == "input":
            # user is on an active HDMI/TV input: hands off. Leaving the input
            # later counts as fresh intent, so the client may come back then.
            session = False
        else:
            # back on SmartCast: the client may launch again
            if not session and tries < 5:
                tries += 1
                try:
                    api("PUT", "/app/launch",
                        {"VALUE": {"MESSAGE": JF_URL, "NAME_SPACE": 4, "APP_ID": "3010"}})
                    log(f"launch sent (try {tries})")
                except Exception as e:  # noqa: BLE001 — never let a failed launch kill the watcher
                    log("launch err: " + repr(e)[:120])
                time.sleep(15)  # give webview time to load before verifying
                beat()
            elif not session and tries == 5:
                log("5 failed tries; quiet until next boot")
                tries += 1  # past cap: stays quiet until a real boot resets this
        time.sleep(4)


if __name__ == "__main__":
    main()
