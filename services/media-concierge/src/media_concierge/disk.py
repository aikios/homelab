"""Disk reporting + reclaim planning for the single /media volume."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .clients.arr import Radarr, Sonarr
from .clients.jellyfin import Jellyfin
from .config import CFG

GB = 1024 ** 3


@dataclass
class TitleSize:
    kind: str          # movie | show
    title: str
    year: int | None
    path: str
    bytes: int
    played: bool
    added_ts: float


def _du(path: str) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def free_gb() -> float:
    return shutil.disk_usage(CFG.media_movies).free / GB


def df() -> dict:
    u = shutil.disk_usage(CFG.media_movies)
    return {"total_gb": round(u.total / GB, 1), "used_gb": round(u.used / GB, 1),
            "free_gb": round(u.free / GB, 1), "pct_used": round(100 * u.used / u.total, 1)}


def inventory_sizes() -> list[TitleSize]:
    """Every library title with on-disk size + played state, from Jellyfin metadata."""
    jf = Jellyfin()
    played = jf.played_map()
    out: list[TitleSize] = []
    for it in jf.items(("Movie", "Series")):
        path = it.get("Path") or ""
        if not path:
            continue
        kind = "movie" if it["Type"] == "Movie" else "show"
        # played: movie -> its own; show -> all episodes played
        if kind == "movie":
            is_played = played.get(it["Id"], False)
        else:
            is_played = False
        added = it.get("DateCreated", "")
        added_ts = 0.0
        try:
            from datetime import datetime
            added_ts = datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        out.append(TitleSize(kind, it["Name"], it.get("ProductionYear"), path,
                             _du(path), is_played, added_ts))
    return out


def reclaim_plan(target_gb: float | None = None) -> dict:
    """Ordered list of deletions to reach target free space: played first, then oldest."""
    target = target_gb if target_gb is not None else CFG.min_free_gb
    cur = free_gb()
    need = max(0.0, target - cur)
    items = inventory_sizes()
    # rank: played before unplayed; within each, oldest added first, largest as tiebreak
    items.sort(key=lambda t: (not t.played, t.added_ts, -t.bytes))
    plan, freed = [], 0.0
    for t in items:
        if freed >= need:
            break
        plan.append({"kind": t.kind, "title": t.title, "year": t.year,
                     "gb": round(t.bytes / GB, 2), "played": t.played, "path": t.path})
        freed += t.bytes / GB
    return {"free_gb": round(cur, 1), "target_gb": target, "need_gb": round(need, 1),
            "would_free_gb": round(freed, 1), "plan": plan,
            "enough": freed >= need or need == 0}


def execute_reclaim(titles: list[dict]) -> list[str]:
    """Delete the given titles via Radarr/Sonarr (which trash files). Returns log lines."""
    r, s = Radarr(), Sonarr()
    rmap = {(m["title"], m.get("year")): m["id"] for m in r.list()}
    smap = {sr["title"]: sr["id"] for sr in s.list()}
    log = []
    for t in titles:
        try:
            if t["kind"] == "movie":
                mid = rmap.get((t["title"], t.get("year")))
                if mid:
                    r.delete(mid, delete_files=True)
                    log.append(f"deleted movie: {t['title']} ({t.get('year')})")
                else:
                    _trash(t["path"]); log.append(f"trashed orphan movie path: {t['path']}")
            else:
                sid = smap.get(t["title"])
                if sid:
                    s.delete(sid, delete_files=True)
                    log.append(f"deleted show: {t['title']}")
                else:
                    _trash(t["path"]); log.append(f"trashed orphan show path: {t['path']}")
        except Exception as e:  # noqa: BLE001
            log.append(f"FAILED {t['title']}: {e}")
    return log


def _trash(path: str) -> None:
    subprocess.run(["trash", path], check=False)
