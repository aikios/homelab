#!/usr/bin/env python3
"""Wire FlareSolverr into Prowlarr and add the Cloudflare-protected indexers.

Idempotent. Adds:
  - indexer proxy "FlareSolverr" -> http://flaresolverr:8191, tag `flare`
  - indexers 1337x, EZTV with that tag
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from mediabot.clients.arr import Prowlarr  # noqa: E402

FLARE_URL = "http://flaresolverr:8191"
TAG = "flare"
CF_INDEXERS = ["1337x", "EZTV"]


def get_or_make_tag(p: Prowlarr) -> int:
    for t in p._j("GET", "/tag"):  # noqa: SLF001
        if t["label"] == TAG:
            return t["id"]
    return p._j("POST", "/tag", json={"label": TAG})["id"]  # noqa: SLF001


def ensure_proxy(p: Prowlarr, tag_id: int) -> None:
    existing = p._j("GET", "/indexerproxy")  # noqa: SLF001
    if any(x["name"] == "FlareSolverr" for x in existing):
        print("  proxy FlareSolverr: already present")
        return
    schema = p._j("GET", "/indexerproxy/schema")  # noqa: SLF001
    fs = next(x for x in schema if x["implementation"] == "FlareSolverr")
    body = dict(fs)
    body["name"] = "FlareSolverr"
    body["tags"] = [tag_id]
    for f in body["fields"]:
        if f["name"] == "host":
            f["value"] = FLARE_URL
        if f["name"] == "requestTimeout":
            f["value"] = 60
    p._j("POST", "/indexerproxy", json=body)  # noqa: SLF001
    print("  proxy FlareSolverr: ADDED ->", FLARE_URL)


def add_indexers(p: Prowlarr, tag_id: int) -> None:
    have = {i["name"].lower() for i in p.indexers()}
    schema = {s["name"].lower(): s for s in p._j("GET", "/indexer/schema")}  # noqa: SLF001
    app_id = p._j("GET", "/appprofile")[0]["id"]  # noqa: SLF001
    for name in CF_INDEXERS:
        if name.lower() in have:
            print(f"  indexer {name}: already present")
            continue
        s = schema.get(name.lower())
        if not s:
            print(f"  indexer {name}: no schema, skip")
            continue
        body = dict(s)
        body["name"] = name
        body["enable"] = True
        body["appProfileId"] = app_id
        body["tags"] = [tag_id]
        for f in body.get("fields", []):
            if f.get("value") in (None, "") and f.get("selectOptions"):
                f["value"] = f["selectOptions"][0]["value"]
        try:
            p._j("POST", "/indexer", json=body)  # noqa: SLF001
            print(f"  indexer {name}: ADDED (via FlareSolverr)")
        except Exception as e:  # noqa: BLE001
            print(f"  indexer {name}: FAILED -> {str(e)[:250]}")


def main() -> None:
    p = Prowlarr()
    tag_id = get_or_make_tag(p)
    print(f"tag {TAG!r} id={tag_id}")
    ensure_proxy(p, tag_id)
    add_indexers(p, tag_id)
    print("\nfinal indexers:")
    for i in p.indexers():
        print(f"  {i['name']:<16} enable={i['enable']} tags={i.get('tags')}")


if __name__ == "__main__":
    main()
