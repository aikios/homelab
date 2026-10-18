"""qBittorrent-wide status monitor.

Driven by the bot poller each cycle (see bot._poll_once). Unlike the per-job
notifications (which track only bot-initiated grabs/magnets and report library
import + subtitles), this watches EVERY torrent in qBittorrent — however it was
added — and emits owner DMs for:

  * added        — a torrent appears that we haven't seen before
  * progress      — a consolidated summary of all active downloads, every
                    QBMON_SUMMARY_MIN minutes, but only while something is downloading
  * stalled       — a download makes no progress for QBMON_STALL_MIN minutes
  * completed     — a torrent reaches 100% (suppressed when a bot job already
                    reports the richer "imported into library" message)

State is keyed by infohash in state/qbmon.json so edges fire once and survive
restarts; on the very first run existing torrents are recorded silently (no
"added" burst).
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from . import jobs
from .clients.qbit import Qbit
from .config import CFG

_PATH = CFG.state_dir / "qbmon.json"

SUMMARY_SECS = int(os.getenv("QBMON_SUMMARY_MIN", "5")) * 60
STALL_SECS = int(os.getenv("QBMON_STALL_MIN", "10")) * 60

# qBittorrent states that mean "this torrent is trying to download"
_DL_STATES = {"downloading", "forcedDL", "metaDL", "stalledDL", "queuedDL",
              "checkingDL", "allocating", "checkingResumeData"}
_PAUSED = {"pausedDL", "stoppedDL", "pausedUP", "stoppedUP"}


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_eta(secs: int) -> str:
    if not secs or secs <= 0 or secs >= 8640000:
        return "—"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict[str, Any]) -> None:
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(_PATH)


def _covered_by_job(infohash: str, name: str) -> bool:
    """True if an open bot job already tracks this torrent (so its own handler will
    post the import result — don't double-announce completion)."""
    try:
        open_jobs = jobs.open_jobs()
    except Exception:  # noqa: BLE001
        return False
    key = name.split(" (")[0].lower()
    for j in open_jobs:
        if j.kind == "magnet" and (j.detail.get("infohash") or "").lower() == infohash:
            return True
        if j.kind == "download" and j.title.split(" (")[0].lower() in name.lower():
            return True
        if key and key in j.title.lower():
            return True
    return False


def tick(notify: Callable[[str], None], torrents: list[dict] | None = None,
         now: float | None = None) -> None:
    """Run one monitor cycle. `notify(text)` posts to the owner. `torrents` and `now`
    are injectable for testing; otherwise fetched/live."""
    if torrents is None:
        try:
            torrents = Qbit().torrents()
        except Exception:  # noqa: BLE001
            return
    now = now or time.time()
    st = _load()
    seeded = bool(st.get("seeded"))
    seen: dict[str, dict] = st.get("t", {})
    msgs: list[str] = []
    cur: set[str] = set()

    for t in torrents:
        h = t.get("hash", "").lower()
        if not h:
            continue
        cur.add(h)
        name = t.get("name", h)
        pct = (t.get("progress") or 0) * 100
        state = t.get("state", "")
        rec = seen.get(h)

        if rec is None:                                   # first time we see it
            seen[h] = {"name": name, "pct": pct, "pct_ts": now, "state": state,
                       "done": pct >= 100, "stalled": False}
            if seeded and pct < 100 and state not in _PAUSED:
                msgs.append(f"➕ added: {name}")
            continue

        if pct > rec["pct"] + 0.01:                       # progressed
            rec["pct"] = pct
            rec["pct_ts"] = now
            rec["stalled"] = False
        rec["name"] = name
        rec["state"] = state

        if pct >= 100 and not rec["done"]:                # completed
            rec["done"] = True
            rec["stalled"] = False
            if not _covered_by_job(h, name):
                msgs.append(f"✅ finished downloading: {name}")
            continue

        if pct < 100 and state not in _PAUSED:            # stall check
            idle = now - rec["pct_ts"]
            if idle >= STALL_SECS and not rec["stalled"]:
                rec["stalled"] = True
                seeds = t.get("num_complete", t.get("num_seeds", "?"))
                mins = int(idle // 60)
                msgs.append(f"⚠️ stalled: {name} — stuck at {pct:.0f}% for {mins}m "
                            f"({state}, {seeds} seeds)")

    for h in [h for h in seen if h not in cur]:           # prune removed torrents
        seen.pop(h, None)

    active = [t for t in torrents
              if (t.get("progress") or 0) < 1.0 and t.get("state") in _DL_STATES]
    if active and now - st.get("last_summary_ts", 0) >= SUMMARY_SECS:
        lines = [f"📥 downloads ({len(active)}):"]
        for t in sorted(active, key=lambda x: -(x.get("progress") or 0)):
            seeds = t.get("num_complete", t.get("num_seeds", 0))
            lines.append(f"  {(t.get('progress') or 0) * 100:5.1f}%  "
                         f"{_fmt_bytes(t.get('dlspeed', 0))}/s  {seeds}s  "
                         f"{_fmt_eta(t.get('eta', 0))}  {t.get('name', '?')[:48]}")
        msgs.append("\n".join(lines))
        st["last_summary_ts"] = now

    st["t"] = seen
    st["seeded"] = True
    _save(st)

    for m in msgs:
        try:
            notify(m)
        except Exception:  # noqa: BLE001
            pass
