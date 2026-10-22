"""Checks + benchmark for subtitle auto-sync (mediabot.subs.sync_timing).

Run with no args for fail-safe self-checks (no media needed); these must pass before
committing. Pass a video + subtitle to benchmark a real sync and see the shift applied.

    python scripts/check_subsync.py
    python scripts/check_subsync.py "/path/video.mkv" "/path/unsynced.srt"
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import types
from pathlib import Path

from mediabot import subs
from mediabot.config import CFG

_TS = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->")


def _first_cue_seconds(srt: str) -> float | None:
    m = _TS.search(Path(srt).read_text(errors="replace"))
    if not m:
        return None
    h, mn, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + s + ms / 1000


def self_checks() -> None:
    # 1) auto-sync disabled -> original path returned untouched
    subs.CFG = types.SimpleNamespace(subs_autosync=False, ffmpeg=CFG.ffmpeg)
    assert subs.sync_timing("/no/such/video.mkv", "/tmp/orig.srt") == "/tmp/orig.srt"

    # 2) enabled but input is bogus -> fail-safe returns the original path, never raises
    subs.CFG = types.SimpleNamespace(subs_autosync=True, ffmpeg=CFG.ffmpeg)
    d = Path(tempfile.mkdtemp())
    srt = d / "x.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert subs.sync_timing("/no/such/video.mkv", str(srt)) == str(srt)

    subs.CFG = CFG  # restore the real config
    print("self-checks PASSED")


def benchmark(video: str, srt: str) -> None:
    before = _first_cue_seconds(srt)
    t = time.time()
    out = subs.sync_timing(video, srt)
    dt = time.time() - t
    changed = out != srt
    after = _first_cue_seconds(out) if changed else before
    shift = (after - before) if (before is not None and after is not None) else None
    print(f"sync {'applied' if changed else 'NOT applied (fail-safe/no-op)'} in {dt:.1f}s")
    print(f"  output: {out}")
    if shift is not None:
        print(f"  first-cue timing shift: {shift:+.3f}s")


if __name__ == "__main__":
    self_checks()
    if len(sys.argv) == 3:
        benchmark(sys.argv[1], sys.argv[2])
