"""Subtitles.

Priority for an English track, so timing always matches the video:
  1. embedded text track in the file itself  (came with the release — always synced)
  2. sidecar .srt/.ass next to the file        (from the same torrent — synced)
  3. online fetch via subliminal               (last resort; may need manual nudging)

Whatever the source, the track we add is normalised to a styled .ass:
yellow fill, black outline, Helvetica, italic (tweak via SUBS_* in .env). Muxed as a
new DEFAULT subtitle track (mkv); the same styled .ass is what `burn` bakes in.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from babelfish import Language

from . import jobs
from .config import CFG, ROOT

_BURN_LOCK = CFG.state_dir / "burn.lock"
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
FONT_DIR = ROOT / "fonts"
_FONT_MIME = "application/vnd.ms-opentype"


def _font_files() -> list[Path]:
    return sorted(p for p in FONT_DIR.glob("*") if p.suffix.lower() in (".otf", ".ttf"))


def _attachment_names(video: str) -> set[str]:
    out = subprocess.run(
        [CFG.ffprobe, "-v", "error", "-select_streams", "t",
         "-show_entries", "stream_tags=filename", "-of", "csv=p=0", video],
        capture_output=True, text=True, check=False)
    return {x.strip().lower() for x in out.stdout.splitlines() if x.strip()}


def ensure_fonts(video: str) -> bool:
    """Attach the bundled font faces to an mkv if they're not already there.
    Uses mkvpropedit (in-place header edit, ~1-2s). Returns True if it added any."""
    fonts = _font_files()
    if not fonts or not video.lower().endswith(".mkv"):
        return False
    have = _attachment_names(video)
    missing = [f for f in fonts if f.name.lower() not in have]
    if not missing:
        return False
    cmd = ["mkvpropedit", video]
    for f in missing:
        cmd += ["--attachment-mime-type", _FONT_MIME, "--add-attachment", str(f)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode not in (0, 1):  # 1 = warnings only
        raise RuntimeError(f"mkvpropedit failed: {r.stderr[-300:] or r.stdout[-300:]}")
    return True


# ───────────────────────────  probing / source pick  ───────────────────────────

def probe(video: str) -> dict:
    out = subprocess.run(
        [CFG.ffprobe, "-v", "error", "-print_format", "json",
         "-show_streams", "-select_streams", "s", video],
        capture_output=True, text=True, check=False)
    embedded = []
    try:
        for s in json.loads(out.stdout or "{}").get("streams", []):
            tags = s.get("tags", {})
            embedded.append({"index": s["index"], "rel_index": len(embedded),
                             "codec": s.get("codec_name"),
                             "lang": (tags.get("language") or "und").lower(),
                             "title": tags.get("title", ""),
                             "forced": bool(s.get("disposition", {}).get("forced"))})
    except json.JSONDecodeError:
        pass
    p = Path(video)
    sidecar = [str(f) for f in p.parent.glob(p.stem + "*") if f.suffix.lower() in SUB_EXT]
    # also look one level up in case arr kept subs beside the source
    sidecar += [str(f) for f in p.parent.glob("*") if f.suffix.lower() in SUB_EXT
                and str(f) not in sidecar]
    return {"video": video, "embedded": embedded, "sidecar": sidecar}


def _alpha3(lang: str) -> str:
    for fn in (Language.fromietf, Language):
        try:
            return fn(lang).alpha3
        except Exception:  # noqa: BLE001
            continue
    return lang.lower()


def _lang_match(tag: str, lang: str) -> bool:
    tag = tag.lower()
    return tag in (lang.lower(), _alpha3(lang)) or (lang == "en" and tag.startswith("eng"))


def has_lang(video: str, lang: str) -> bool:
    info = probe(video)
    if any(_lang_match(e["lang"], lang) and e["codec"] in TEXT_SUB_CODECS
           for e in info["embedded"]):
        return True
    return any(_lang_match(Path(s).stem.split(".")[-1], lang) for s in info["sidecar"])


STYLED_TITLE = "English (styled)"


def _current_style_line(video: str) -> str | None:
    """The `Style: Default,...` line of the file's first subtitle stream, if extractable."""
    raw = CFG.state_dir / "stylecheck.ass"
    r = subprocess.run([CFG.ffmpeg, "-v", "error", "-y", "-i", video,
                        "-map", "0:s:0", "-c:s", "copy", str(raw)],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0 or not raw.exists():
        return None
    line = None
    for ln in raw.read_text(errors="replace").splitlines():
        if ln.startswith("Style: Default"):
            line = ln.strip()
            break
    raw.unlink(missing_ok=True)
    return line


def needs_restyle(video: str) -> bool:
    """True if the embedded styled track's style doesn't match the current house style."""
    cur = _current_style_line(video)
    return cur is None or cur != _style_line()


def pick_source(video: str, lang: str = "en") -> tuple[str, str] | None:
    """Return (kind, ref): ('done','') if our styled track is already present |
    ('embedded', rel_index) | ('sidecar', path) | ('online', path).
    Tries release-bundled options before going online."""
    info = probe(video)
    for e in info["embedded"]:
        if e["codec"] == "ass" and e["title"] == STYLED_TITLE:
            return ("done", "")
    for e in info["embedded"]:
        if _lang_match(e["lang"], lang) and e["codec"] in TEXT_SUB_CODECS and not e["forced"]:
            return ("embedded", str(e["rel_index"]))
    for s in info["sidecar"]:
        stem_tail = Path(s).stem.lower()
        if _lang_match(stem_tail.split(".")[-1], lang) or f".{lang}" in stem_tail or \
           (lang == "en" and "english" in stem_tail):
            return ("sidecar", s)
    # only-one sidecar with no language marker: assume it's the release's main sub
    bare = [s for s in info["sidecar"] if "." not in Path(s).stem.split("]")[-1]]
    if len(info["sidecar"]) == 1:
        return ("sidecar", info["sidecar"][0])
    got = _fetch_online(video, lang)
    return ("online", got) if got else None


# ───────────────────────────  online fallback  ───────────────────────────

def _fetch_online(video: str, lang: str = "en") -> str | None:
    from subliminal import (download_best_subtitles, region, save_subtitles,  # noqa: PLC0415
                            scan_video)
    region.configure("dogpile.cache.dbm",
                     arguments={"filename": str(CFG.state_dir / "subliminal.dbm")},
                     replace_existing_backend=True)
    pc = {}
    if CFG.opensubtitles_api_key:
        pc["opensubtitlescom"] = {"apikey": CFG.opensubtitles_api_key}
    v = scan_video(video)
    subs = download_best_subtitles({v}, {Language(_alpha3(lang))},
                                   provider_configs=pc or None)
    got = subs.get(v, [])
    if not got:
        return None
    save_subtitles(v, got)
    p = Path(video)
    for pat in (f"*{_alpha3(lang)}*", f"*{lang}*", "*"):
        hits = [f for f in p.parent.glob(p.stem + pat) if f.suffix.lower() == ".srt"]
        if hits:
            return str(max(hits, key=lambda f: f.stat().st_mtime))
    return None


# ───────────────────────────  styling  ───────────────────────────

def _style_line() -> str:
    # ASS colours are &HAABBGGRR.  BorderStyle 1 = outline+shadow (no box).
    return ("Style: Default,{font},{size},{primary},&H000000FF,{outline},&H64000000,"
            "0,{italic},0,0,100,100,0,0,1,{ow},0,2,40,40,{mv},1").format(
        font=CFG.subs_font, size=CFG.subs_size, primary=CFG.subs_primary,
        outline=CFG.subs_outline, italic=-1 if CFG.subs_italic else 0,
        ow=CFG.subs_outline_w, mv=CFG.subs_margin_v)


def to_styled_ass(sub_or_video: str, embedded_rel_index: str | None = None,
                  out_dir: str | None = None) -> str:
    """Produce a .ass with our house style. Source can be a .srt/.ass path, or a video
    plus the relative index of an embedded text track to extract."""
    dest_dir = Path(out_dir) if out_dir else Path(sub_or_video).parent
    raw = dest_dir / (Path(sub_or_video).stem + ".raw.ass")
    if embedded_rel_index is not None:
        cmd = [CFG.ffmpeg, "-y", "-i", sub_or_video, "-map", f"0:s:{embedded_rel_index}",
               "-c:s", "ass", str(raw)]
    else:
        cmd = [CFG.ffmpeg, "-y", "-i", sub_or_video, str(raw)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not raw.exists():
        raise RuntimeError(f"sub->ass convert failed: {r.stderr[-400:]}")

    text = raw.read_text(encoding="utf-8", errors="replace")
    # 1080p reference resolution
    if "PlayResX" not in text:
        text = text.replace("[Script Info]", "[Script Info]\nPlayResX: 1920\nPlayResY: 1080", 1)
    else:
        text = re.sub(r"PlayResX:.*", "PlayResX: 1920", text)
        text = re.sub(r"PlayResY:.*", "PlayResY: 1080", text)
    # replace the whole [V4+ Styles] block with just our Default style
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
           "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
           "MarginR, MarginV, Encoding")
    styles_block = f"[V4+ Styles]\n{fmt}\n{_style_line()}\n"
    text = re.sub(r"\[V4\+ Styles\].*?(?=\n\[)", styles_block, text, flags=re.S)
    # force every dialogue line onto Default (drop imported style names / karaoke)
    text = re.sub(r"^(Dialogue:[^,]*,[^,]*,[^,]*,)[^,]*,", r"\1Default,", text, flags=re.M)
    styled = dest_dir / (Path(sub_or_video).stem + ".styled.ass")
    styled.write_text(text, encoding="utf-8")
    raw.unlink(missing_ok=True)
    return str(styled)


# ───────────────────────────  attach  ───────────────────────────

def remux_to_mkv(video: str) -> str:
    """Container swap mp4/etc -> mkv, stream copy (no re-encode). Drops source
    subtitle streams (mov_text won't fit mkv and we replace subs anyway). Deletes the
    original and returns the new .mkv path."""
    src = Path(video)
    out = src.with_suffix(".mkv")
    if out.exists() and out != src:
        out = src.with_name(src.stem + ".remux.mkv")
    cmd = [CFG.ffmpeg, "-y", "-i", video, "-map", "0:v", "-map", "0:a?",
           "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 1_000_000:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"remux to mkv failed: {r.stderr[-400:]}")
    if out != src:
        src.unlink(missing_ok=True)
        final = src.with_suffix(".mkv")
        if out != final:
            out.replace(final)
            out = final
    return str(out)


def mux_styled(video: str, ass_path: str, lang: str = "en") -> str:
    """Rewrite as: video + audio + ONE subtitle = our styled .ass, default.

    All other embedded subs are dropped on purpose — keeping 22 language tracks is
    what let players pick the wrong (unstyled) one. Re-running is safe/idempotent.
    Non-mkv (mp4, m4v, avi, ...) is remuxed to mkv first (stream copy, no re-encode).
    Returns the final path (which changes if a remux happened).
    """
    src = Path(video)
    if src.suffix.lower() != ".mkv":
        video = remux_to_mkv(video)
        src = Path(video)
    tmp = src.with_suffix(".muxing.mkv")
    a3 = _alpha3(lang)
    # ASS is input 0 so it is deterministically output subtitle stream 0.
    cmd = [CFG.ffmpeg, "-y", "-i", ass_path, "-i", video,
           "-map", "1:v", "-map", "1:a?", "-map", "0:0",
           "-c", "copy",
           "-metadata:s:s:0", f"language={a3}",
           "-metadata:s:s:0", "title=English (styled)",
           "-disposition:s:0", "default",
           str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"mux failed: {r.stderr[-500:]}")
    os.replace(tmp, video)
    Path(ass_path).unlink(missing_ok=True)
    return video


def burn_async(video: str, ass_path: str, job_id: str) -> None:
    def _run() -> None:
        import time
        while _BURN_LOCK.exists():
            time.sleep(30)
        _BURN_LOCK.write_text(job_id)
        try:
            src = Path(video)
            out = src.with_name(src.stem + ".subbed" + src.suffix)
            esc = ass_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            cmd = ["nice", "-n", "19", CFG.ffmpeg, "-y", "-i", video,
                   "-vf", f"ass='{esc}'", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "20", "-c:a", "copy", "-map", "0:v:0", "-map", "0:a?",
                   str(out)]
            jobs.update(job_id, last_report="burning… hours on this box")
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode != 0 or not out.exists():
                jobs.update(job_id, state="error", last_report=f"burn failed: {r.stderr[-300:]}")
                return
            os.replace(out, video)
            Path(ass_path).unlink(missing_ok=True)
            jobs.update(job_id, state="done", last_report=f"burned {src.name}")
        finally:
            _BURN_LOCK.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True, name=f"burn-{job_id}").start()
