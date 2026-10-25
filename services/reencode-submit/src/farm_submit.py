#!/usr/bin/env python3
# farm_submit.py -- blacklodge-side driver for the vega re-encode farm.
# Usage:
#   farm_submit.py <media-file> [more files...]   submit explicit paths
#   farm_submit.py --scan                         queue every Jellyfin item
#                                                 that is not direct-playable
#   farm_submit.py --list                         show queue/claimed/done/failed
# Jobs land in ~/reencode_farm/queue/ and are claimed atomically by the vega
# worker. Re-running --scan is safe: anything already queued/claimed/done is
# skipped (failed jobs are retried).

import glob
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import farm_notify


def _load_env():
    """This is a plain CLI (no systemd EnvironmentFile), so load the service's
    gitignored .env ourselves if present. Existing env wins over the file."""
    envfile = Path(__file__).resolve().parent.parent / ".env"
    if not envfile.exists():
        return
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

# Config from the environment / per-service .env (see .env.example); no key in
# this public tree. JF defaults to the local Jellyfin (submit runs on the server).
JF = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
BASE = os.path.expanduser(os.environ.get("FARM_QUEUE_DIR", "~/reencode_farm"))
STATES = ("queue", "claimed", "done")
# A blind scan can catch a file mid-import/remux (Jellyfin may even report a
# transient path that then vanishes). Don't queue anything that isn't settled:
# missing, zero-size, or modified within this window. It gets picked up on a
# later scan once stable.
STABLE_SECS = int(os.environ.get("FARM_STABLE_SECS", "120"))


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line)
    with open(os.path.join(BASE, "submit.log"), "a") as f:
        f.write(line + "\n")


def api(path):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(JF + path + sep + "api_key=" + API_KEY, timeout=30) as r:
        return json.load(r)


def known_sources():
    srcs = set()
    for state in STATES:
        for fn in glob.glob(os.path.join(BASE, state, "*.json")):
            try:
                with open(fn) as f:
                    s = json.load(f).get("source")
                    if s:
                        srcs.add(s)
            except (ValueError, OSError):
                pass
    return srcs


def submit(path, **extra):
    path = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        log("SKIP (missing): " + path)
        return None
    if st.st_size == 0:
        log("SKIP (zero-size): " + path)
        return None
    age = time.time() - st.st_mtime
    if age < STABLE_SECS:
        log(f"SKIP (still settling, {age:.0f}s < {STABLE_SECS}s): " + path)
        return None
    job = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + hex(abs(hash(path)))[2:8],
           "source": path,
           "source_bytes": st.st_size,
           "replace": True,
           "extract_subs": True,
           "refresh": True}
    job.update(extra)
    fn = os.path.join(BASE, "queue", job["id"] + ".json")
    with open(fn, "w") as f:
        json.dump(job, f, indent=1)
    log("submitted: " + path)
    return job


def scan():
    items = api("/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
                "&Fields=MediaSources,Path")["Items"]
    have = known_sources()
    queued = []
    for it in items:
        ms = (it.get("MediaSources") or [{}])[0]
        path = ms.get("Path") or it.get("Path")
        if not path:
            continue
        streams = ms.get("MediaStreams") or []
        v = next((s for s in streams if s.get("Type") == "Video"), {})
        aidx = ms.get("DefaultAudioStreamIndex")
        a = next((s for s in streams if s.get("Type") == "Audio" and
                  s.get("Index") == aidx), None) or \
            next((s for s in streams if s.get("Type") == "Audio"), {})
        friendly = (ms.get("Container") == "mp4" and v.get("Codec") == "h264"
                    and a.get("Codec") == "aac")
        if friendly or path in have:
            continue
        why = (f"{ms.get('Container') or '?'} · "
               f"{v.get('Codec') or '?'}/{a.get('Codec') or '?'}")
        job = submit(path, why=why)
        if job:
            queued.append(job)
    log("scan complete: " + str(len(queued)) + " job(s) submitted")
    farm_notify.notify_submitted(queued)
    # backstop for a missed reencode-notify.path trigger; no-op if nothing new
    farm_notify.notify_results()
    return len(queued)


def show():
    for state in ("queue", "claimed", "done", "failed"):
        files = sorted(glob.glob(os.path.join(BASE, state, "*.json")))
        print(state + ": " + str(len(files)))
        for fn in files[-5:]:
            try:
                with open(fn) as f:
                    print("   " + json.load(f).get("source", "?"))
            except (ValueError, OSError):
                print("   " + os.path.basename(fn) + " (unparsable)")


def main():
    os.makedirs(os.path.join(BASE, "queue"), exist_ok=True)
    for d in ("claimed", "done", "failed"):
        os.makedirs(os.path.join(BASE, d), exist_ok=True)
    args = sys.argv[1:]
    if not args:
        show()
        return
    if args[0] == "--scan":
        scan()
    elif args[0] == "--list":
        show()
    else:
        farm_notify.notify_submitted([j for j in map(submit, args) if j])


if __name__ == "__main__":
    main()