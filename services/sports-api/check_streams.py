#!/usr/bin/env python3
"""Pull a real segment of every working candidate THROUGH the proxy, the way
the TV would, and report what the bytes actually are.

    python3 check_streams.py http://localhost:8100 /nfl/redzone

"ts@0" is what the TV can play; "ts@N" means a disguised segment the proxy
failed to unwrap; "IMAGE" a decoy. Stdlib only.
"""
import json
import re
import sys
import urllib.request

api, path = sys.argv[1].rstrip("/"), sys.argv[2]


def get(u, n=None):
    with urllib.request.urlopen(u, timeout=20) as r:
        return r.read(n) if n else r.read()


def ts_at(b):
    for i in range(min(len(b) - 376, 65536)):
        if b[i] == 0x47 and b[i + 188] == 0x47 and b[i + 376] == 0x47:
            return i
    return None


d = json.loads(get("%s/prepare?path=%s&wait=40" % (api, path)))
print("%s: %d/%d ok, done=%s via=%s" % (d["title"], d["ok_count"], len(d["sources"]),
                                        d["done"], d.get("via", "local")))
good = bad = 0
for s in d["sources"]:
    for c in s["candidates"]:
        base = re.match(r"(http://[^/]+)", c["url"]).group(1)
        try:
            body, url = get(c["url"]), c["url"]
            for _ in range(2):
                uris = [x for x in body.decode().splitlines() if x and not x.startswith("#")]
                url = base + (uris[0] if b"#EXT-X-STREAM-INF" in body else uris[-1])
                body = get(url, 65536)
                if body[:7] != b"#EXTM3U":
                    break
            off = ts_at(body)
            kind = ("ts@%d" % off) if off is not None else (
                "IMAGE" if body[:4] in (b"\x89PNG", b"RIFF") else "other:%r" % body[:4])
        except Exception as e:
            kind = "ERR %r" % e
        good += kind == "ts@0"
        bad += kind != "ts@0"
        print("  %-30s %s" % (s["url"][8:38], kind))
print("playable ts@0: %d, other: %d" % (good, bad))
