"""Picking the primary subtitle track out of several.

Releases often ship several tracks in one language with very different coverage:
a full dialogue track, an SDH track (dialogue + sound cues), and a "forced" /
signs-and-songs track that only covers on-screen text. Services that keep one
subtitle per language (media-concierge's styled track, media-transcoder's sidecar
extraction) call :func:`pick_primary` so they agree on which one that is.

Stdlib only: callers supply the tracks (from ffprobe) and, where they have them,
cue counts (from the mkv NUMBER_OF_FRAMES statistics tag, or :func:`cue_count`
on the extracted text).
"""
from __future__ import annotations

import re

# Words in a track title that mark it as partial coverage, never the primary.
SECONDARY_WORDS = ("forced", "sign", "song", "commentary", "karaoke", "lyrics")
SDH_RE = re.compile(r"\b(sdh|cc|hi|hearing[ -]impaired|deaf)\b", re.IGNORECASE)

# alpha2 <-> alpha3 (both bibliographic and terminology forms) for the languages
# this library actually sees; anything else compares as-is.
_LANG_GROUPS = (
    ("en", "eng"), ("es", "spa"), ("fr", "fre", "fra"), ("de", "ger", "deu"),
    ("it", "ita"), ("ja", "jpn"), ("pt", "por"), ("zh", "chi", "zho"),
    ("ko", "kor"), ("ru", "rus"), ("nl", "dut", "nld"), ("sv", "swe"),
    ("ar", "ara"), ("hi", "hin"), ("pl", "pol"), ("tr", "tur"),
)
_LANG_KEY = {code: group[0] for group in _LANG_GROUPS for code in group}


def lang_key(tag: str) -> str:
    """Normalise a language tag ("eng", "en", "en-US", "ENG") to one comparable key."""
    t = (tag or "und").lower().split("-")[0].split("_")[0]
    return _LANG_KEY.get(t, t)


def cue_count(text: str) -> int:
    """Number of subtitle events in SRT / WebVTT / ASS text."""
    ass = sum(1 for ln in text.splitlines() if ln.startswith("Dialogue:"))
    return ass or text.count("-->")


def is_secondary(track: dict) -> bool:
    """Forced / signs-and-songs / commentary: partial coverage by design."""
    title = (track.get("title") or "").lower()
    return bool(track.get("forced")) or any(w in title for w in SECONDARY_WORDS)


def is_sdh(track: dict) -> bool:
    return bool(track.get("hearing_impaired")) or bool(SDH_RE.search(track.get("title") or ""))


def pick_primary(tracks: list[dict]) -> dict | None:
    """The track that actually carries the dialogue, or None if every track is
    secondary. Each track is a dict with optional `title`, `forced`,
    `hearing_impaired` and `cues` (int, or None when unknown).

    Most cues wins (a forced track has a handful, the full track hundreds); on a
    tie (or when counts are unknown) a plain track beats SDH, then the first
    listed wins, which keeps the release's own order.
    """
    full = [t for t in tracks if not is_secondary(t)]
    if not full:
        return None
    return max(enumerate(full),
               key=lambda it: (it[1].get("cues") or -1, not is_sdh(it[1]), -it[0]))[1]


def frames_tag(tags: dict) -> int | None:
    """mkvmerge's per-track statistics tag (NUMBER_OF_FRAMES, sometimes with a
    language suffix): the cue count for free, without extracting the track."""
    for k, v in (tags or {}).items():
        if k.upper().startswith("NUMBER_OF_FRAMES"):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None
