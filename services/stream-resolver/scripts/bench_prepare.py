#!/usr/bin/env python3
"""Time a progressive /prepare against a running stream-resolver.

    python3 scripts/bench_prepare.py http://localhost:8100 /nfl/401873012

Long-polls the job and prints when each verdict lands; the number that matters
for the TV is "first ok" (time until something is playable). Stdlib only.
"""
import json
import sys
import time
import urllib.request

api, path = sys.argv[1].rstrip("/"), sys.argv[2]
t0, since, first = time.time(), -1, None
while True:
    u = "%s/prepare?path=%s&since=%d&wait=15" % (api, path, since)
    with urllib.request.urlopen(u, timeout=30) as r:
        d = json.load(r)
    since = d["version"]
    if d["ok_count"] and first is None:
        first = time.time() - t0
    print("%5.1fs checked %2d/%d ok %2d%s" % (
        time.time() - t0, d["checked"], len(d["sources"]), d["ok_count"],
        " DONE" if d["done"] else ""))
    if d["done"]:
        break
print("first ok: %s  all done: %.1fs" % (
    "%.1fs" % first if first else "never", time.time() - t0))
