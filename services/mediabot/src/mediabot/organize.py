"""Identify a loose file and file it into the Jellyfin library layout.

Layout Jellyfin/arr expect:
  Movies:  <MEDIA_MOVIES>/<Title> (<Year>)/<Title> (<Year>).<ext>
  Shows:   <MEDIA_SHOWS>/<Title>/Season <NN>/<Title> - S<NN>E<NN>.<ext>
Metadata (cast, plot, art) is scraped by Jellyfin from the folder name + year on scan,
so getting the name/year right is the whole job.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from guessit import guessit

from .clients.arr import Radarr, Sonarr
from .config import CFG

JUNK_NAMES = {".DS_Store"}
JUNK_GLOBS = ["._*", "*.nfo.bak", "*sample*", "*.txt", "*.exe", "*.lnk", "RARBG*", "*.jpg"]
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


@dataclass
class Identity:
    kind: str                      # movie | episode
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    resolution: str | None = None
    source: str | None = None
    confidence: str = "guess"      # guess | matched
    candidates: list[dict] = field(default_factory=list)


def _biggest_video(path: Path) -> Path | None:
    if path.is_file():
        return path if path.suffix.lower() in VIDEO_EXT else None
    vids = [p for p in path.rglob("*") if p.suffix.lower() in VIDEO_EXT and p.is_file()]
    return max(vids, key=lambda p: p.stat().st_size) if vids else None


def identify(path: str, title_hint: str | None = None) -> Identity:
    p = Path(path)
    vid = _biggest_video(p)
    name_for_guess = title_hint or (vid.name if vid else p.name)
    g = guessit(name_for_guess)
    kind = "episode" if g.get("type") == "episode" or g.get("episode") else "movie"
    ident = Identity(
        kind=kind,
        title=str(g.get("title") or title_hint or p.stem),
        year=g.get("year"),
        season=g.get("season"),
        episode=g.get("episode") if isinstance(g.get("episode"), int) else None,
        resolution=g.get("screen_size"),
        source=g.get("source"),
    )
    # confirm against the arr metadata proxies
    try:
        if kind == "movie":
            term = f"{ident.title} {ident.year}" if ident.year else ident.title
            hits = Radarr().lookup(term)[:5]
            ident.candidates = [{"title": h["title"], "year": h.get("year"),
                                 "tmdbId": h.get("tmdbId")} for h in hits]
            if hits:
                best = hits[0]
                ident.title, ident.year = best["title"], best.get("year")
                ident.tmdb_id = best.get("tmdbId")
                ident.confidence = "matched"
        else:
            hits = Sonarr().lookup(ident.title)[:5]
            ident.candidates = [{"title": h["title"], "year": h.get("year"),
                                 "tvdbId": h.get("tvdbId")} for h in hits]
            if hits:
                best = hits[0]
                ident.title = best["title"]
                ident.tvdb_id = best.get("tvdbId")
                ident.confidence = "matched"
    except Exception:  # noqa: BLE001 -- identification is best-effort
        pass
    return ident


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def clean_junk(root: str) -> list[str]:
    removed = []
    rp = Path(root)
    for f in rp.rglob("*"):
        if not f.exists():
            continue
        if f.name in JUNK_NAMES or any(f.match(g) for g in JUNK_GLOBS):
            try:
                f.unlink()
                removed.append(str(f))
            except OSError:
                pass
    # prune now-empty dirs
    for d in sorted((p for p in rp.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
            removed.append(str(d) + "/")
        except OSError:
            pass
    return removed


def import_file(path: str, ident: Identity, copy: bool = False) -> dict:
    """Move/copy the identified video (+ sibling subs) into the library. Returns result dict."""
    src = Path(path)
    vid = _biggest_video(src)
    if not vid:
        raise ValueError(f"no video file found under {path}")

    if ident.kind == "movie":
        folder = Path(CFG.media_movies) / f"{_safe(ident.title)} ({ident.year})"
        base = f"{_safe(ident.title)} ({ident.year})"
    else:
        se = f"S{ident.season or 1:02d}E{ident.episode or 1:02d}"
        folder = Path(CFG.media_shows) / _safe(ident.title) / f"Season {ident.season or 1:02d}"
        base = f"{_safe(ident.title)} - {se}"

    folder.mkdir(parents=True, exist_ok=True)
    dest_vid = folder / f"{base}{vid.suffix.lower()}"
    op = shutil.copy2 if copy else shutil.move
    op(str(vid), str(dest_vid))
    moved = [str(dest_vid)]

    # carry sibling subtitle files, renamed to match + keep language suffix
    for sib in vid.parent.iterdir():
        if sib.suffix.lower() in SUB_EXT and sib.is_file():
            lang = ""
            m = re.search(r"\.([a-z]{2,3})(\.forced)?" + re.escape(sib.suffix) + r"$", sib.name, re.I)
            if m:
                lang = "." + m.group(1).lower() + (m.group(2) or "")
            dest_sub = folder / f"{base}{lang or '.und'}{sib.suffix.lower()}"
            op(str(sib), str(dest_sub))
            moved.append(str(dest_sub))

    junk = clean_junk(str(src)) if src.is_dir() else []
    return {"library_path": str(folder), "files": moved, "cleaned": junk,
            "identity": ident.__dict__}
