"""Tiny persistent job tracker for long-running work (downloads, subtitle burns).

The Discord bot's poller reads OPEN jobs, checks progress via the relevant client,
posts updates to the originating channel, and closes them when done.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import CFG

JobKind = Literal["download", "burn", "import", "magnet"]
JobState = Literal["open", "done", "error"]

_PATH = CFG.state_dir / "jobs.json"


@dataclass
class Job:
    id: str
    kind: JobKind
    title: str
    channel_id: int
    state: JobState = "open"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    detail: dict[str, Any] = field(default_factory=dict)
    last_report: str = ""


def _load() -> dict[str, dict]:
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(d: dict[str, dict]) -> None:
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(_PATH)


def create(kind: JobKind, title: str, channel_id: int, **detail: Any) -> Job:
    j = Job(id=uuid.uuid4().hex[:8], kind=kind, title=title, channel_id=channel_id, detail=detail)
    d = _load()
    d[j.id] = asdict(j)
    _save(d)
    return j


def update(job_id: str, *, state: JobState | None = None, last_report: str | None = None,
           **detail: Any) -> None:
    d = _load()
    if job_id not in d:
        return
    rec = d[job_id]
    if state:
        rec["state"] = state
    if last_report is not None:
        rec["last_report"] = last_report
    rec["detail"].update(detail)
    rec["updated"] = time.time()
    _save(d)


def get(job_id: str) -> Job | None:
    rec = _load().get(job_id)
    return Job(**rec) if rec else None


def open_jobs() -> list[Job]:
    return [Job(**r) for r in _load().values() if r["state"] == "open"]


def all_jobs() -> list[Job]:
    return [Job(**r) for r in sorted(_load().values(), key=lambda r: r["created"], reverse=True)]
