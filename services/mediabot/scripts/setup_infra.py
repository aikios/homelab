#!/usr/bin/env python3
"""Idempotent infra prep for mediabot. Run once (safe to re-run).

- add public indexers to Prowlarr (1337x, YTS, EZTV, TorrentGalaxy) if missing
- turn ON Radarr/Sonarr file renaming + Jellyfin-friendly folder formats
- ensure qBittorrent categories (radarr, sonarr, manual)
- print what still needs a human (Jellyfin API key, Discord owner id)
"""
from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from mediabot.clients.arr import Prowlarr, Radarr, Sonarr  # noqa: E402
from mediabot.clients.qbit import Qbit  # noqa: E402

WANT_INDEXERS = ["1337x", "YTS", "EZTV", "TorrentGalaxy"]


def prowlarr_indexers() -> None:
    p = Prowlarr()
    have = {i["name"].lower() for i in p.indexers()}
    schema = p._j("GET", "/indexer/schema")  # noqa: SLF001
    by_name = {s["name"].lower(): s for s in schema}
    app_profile_id = p._j("GET", "/appprofile")[0]["id"]  # noqa: SLF001
    for name in WANT_INDEXERS:
        if name.lower() in have:
            print(f"  indexer {name}: already present")
            continue
        s = by_name.get(name.lower())
        if not s:
            print(f"  indexer {name}: no schema def, skipping")
            continue
        body = dict(s)
        body["name"] = name
        body["enable"] = True
        body["appProfileId"] = app_profile_id
        for f in body.get("fields", []):
            if f.get("value") in (None, "") and f.get("selectOptions"):
                f["value"] = f["selectOptions"][0]["value"]
        try:
            p._j("POST", "/indexer", json=body)  # noqa: SLF001
            print(f"  indexer {name}: ADDED")
        except Exception as e:  # noqa: BLE001
            print(f"  indexer {name}: add failed -> {str(e)[:200]}")


def arr_naming() -> None:
    for app, kind in ((Radarr(), "movie"), (Sonarr(), "series")):
        cfg = app._j("GET", "/config/naming")  # noqa: SLF001
        if kind == "movie":
            cfg["renameMovies"] = True
            cfg["movieFolderFormat"] = "{Movie Title} ({Release Year})"
            cfg["standardMovieFormat"] = "{Movie Title} ({Release Year}) {Quality Full}"
        else:
            cfg["renameEpisodes"] = True
            cfg["seriesFolderFormat"] = "{Series Title}"
            cfg["seasonFolderFormat"] = "Season {season:00}"
            cfg["standardEpisodeFormat"] = (
                "{Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}")
        app._j("PUT", "/config/naming", json=cfg)  # noqa: SLF001
        print(f"  {kind} naming: rename ON, folder formats set")


def arr_extra_files() -> None:
    """Carry release-bundled .srt/.ass into the library on import (so subtitle timing
    matches the exact video file)."""
    for app, kind in ((Radarr(), "movie"), (Sonarr(), "series")):
        mm = app._j("GET", "/config/mediamanagement")  # noqa: SLF001
        mm["importExtraFiles"] = True
        mm["extraFileExtensions"] = "srt,ass,ssa,sub,idx"
        app._j("PUT", "/config/mediamanagement", json=mm)  # noqa: SLF001
        print(f"  {kind}: import extra files ON (srt,ass,ssa,sub,idx)")


def qbit_categories() -> None:
    q = Qbit()
    have = set(q.categories())
    for c in ("radarr", "sonarr", "manual"):
        if c in have:
            print(f"  qbit category {c}: present")
        else:
            q.add_category(c, f"/media/downloads/{c}")
            print(f"  qbit category {c}: created")


def main() -> None:
    print("Prowlarr indexers:")
    prowlarr_indexers()
    print("arr naming:")
    arr_naming()
    print("arr extra files:")
    arr_extra_files()
    print("qBittorrent categories:")
    qbit_categories()
    print("\nStill needed from you:")
    print("  - JELLYFIN_API_KEY  (Jellyfin Dashboard -> Advanced -> API Keys -> +)")
    print("  - OWNER_DISCORD_ID  (your Discord user id; enable Developer Mode, right-click "
          "your name -> Copy User ID)")
    print("  put both in ~/homelab/secrets/shared.env")


if __name__ == "__main__":
    main()
