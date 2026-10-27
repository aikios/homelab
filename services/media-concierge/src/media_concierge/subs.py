"""Subtitles.

Priority for an English track, so timing always matches the video:
  1. embedded text track in the file itself  (came with the release — always synced)
  2. sidecar .srt/.ass next to the file        (from the same torrent — synced)
  3. online fetch via subliminal               (last resort; may need manual nudging)

When a release carries several tracks in the language (full / SDH / forced /
signs & songs), the primary one (the one with the dialogue) is picked via
homelab_core.subtitles.pick_primary, the same rule media-transcoder uses.

Whatever the source, the track we add is normalised to a styled .ass: yellow fill,
black outline, Helvetica, italic (tweak via MEDIA_CONCIERGE_SUBS_* in .env), written
as a sidecar `<video stem>.<lang>.default.ass` that Jellyfin lists as the default
track. The video file itself is never rewritten, so the mp4 that media-transcoder
makes for the TV stays direct-playable. The same styled .ass is what `burn` bakes in.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from babelfish import Language
from homelab_core import subtitles as subpick

from . import jobs
from .config import CFG, ROOT

_BURN_LOCK = CFG.state_dir / "burn.lock"
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
FONT_DIR = ROOT / "fonts"  # house font, handed to libass when burning
_VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"}


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
            disp = s.get("disposition", {})
            embedded.append({"index": s["index"], "rel_index": len(embedded),
                             "codec": s.get("codec_name"),
                             "lang": (tags.get("language") or "und").lower(),
                             "title": tags.get("title", ""),
                             "forced": bool(disp.get("forced")),
                             "hearing_impaired": bool(disp.get("hearing_impaired")),
                             "cues": subpick.frames_tag(tags)})
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


STYLED_TITLE = "English (styled)"  # legacy: the title of a styled track muxed into an mkv


def styled_sidecar(video: str, lang: str = "en") -> Path:
    """Where our styled track lives: next to the video, flagged default for Jellyfin."""
    p = Path(video)
    return p.with_name(f"{p.stem}.{lang}.default.ass")


def _style_line_of(sub_path: str) -> str | None:
    try:
        text = Path(sub_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for ln in text.splitlines():
        if ln.startswith("Style: Default"):
            return ln.strip()
    return None


def _ours(sub_path: str) -> bool:
    """A styled .ass we produced (its Default style is the current house style)."""
    return sub_path.lower().endswith(".ass") and _style_line_of(sub_path) == _style_line()


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


def needs_restyle(video: str, lang: str = "en") -> bool:
    """True if our styled track (sidecar, or a legacy one muxed into an mkv) doesn't
    match the current house style."""
    side = styled_sidecar(video, lang)
    cur = _style_line_of(str(side)) if side.exists() else _current_style_line(video)
    return cur is None or cur != _style_line()


def _alone_in_dir(video: str) -> bool:
    p = Path(video)
    return sum(1 for f in p.parent.iterdir() if f.suffix.lower() in _VIDEO_EXT) == 1


def _fill_cues(video: str, tracks: list[dict]) -> None:
    """Cue counts for embedded tracks the mkv statistics tags didn't cover: extract
    all of them in ONE ffmpeg pass (each pass reads the whole file) and count."""
    todo = [t for t in tracks if t.get("cues") is None]
    if not todo:
        return
    cmd = [CFG.ffmpeg, "-v", "error", "-y", "-i", video]
    outs = []
    for t in todo:
        out = CFG.state_dir / f"cuecount.{t['rel_index']}.ass"
        cmd += ["-map", f"0:s:{t['rel_index']}", "-c:s", "ass", str(out)]
        outs.append((t, out))
    subprocess.run(cmd, capture_output=True, text=True, check=False)
    for t, out in outs:
        try:
            t["cues"] = subpick.cue_count(out.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            t["cues"] = None
        out.unlink(missing_ok=True)


def pick_source(video: str, lang: str = "en") -> tuple[str, str] | None:
    """Return (kind, ref): ('done','') if our styled track is already present |
    ('embedded', rel_index) | ('sidecar', path) | ('online', path).
    Tries release-bundled options before going online."""
    info = probe(video)
    if styled_sidecar(video, lang).exists():
        return ("done", "")
    for e in info["embedded"]:
        if e["codec"] == "ass" and e["title"] == STYLED_TITLE:
            return ("done", "")
    # several tracks in the language (full / SDH / forced / signs): take the primary
    emb = [e for e in info["embedded"]
           if _lang_match(e["lang"], lang) and e["codec"] in TEXT_SUB_CODECS]
    if emb:
        _fill_cues(video, emb)
        best = subpick.pick_primary(emb)
        if best:
            return ("embedded", str(best["rel_index"]))
    side = []
    # this video's own sidecars; other subs in the folder only when the video is
    # alone there (a season folder's other .en.srt files belong to other episodes)
    own = [s for s in info["sidecar"] if Path(s).name.startswith(Path(video).stem)]
    for s in own or (info["sidecar"] if _alone_in_dir(video) else []):
        stem_tail = Path(s).stem.lower()
        if _lang_match(stem_tail.split(".")[-1], lang) or f".{lang}" in stem_tail or \
           (lang == "en" and "english" in stem_tail):
            if _ours(s):  # a copy of our styled track (e.g. extracted by the transcoder)
                return ("done", "")
            try:
                cues = subpick.cue_count(Path(s).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            side.append({"path": s, "title": Path(s).name, "cues": cues})
    best = subpick.pick_primary(side)
    if best:
        return ("sidecar", best["path"])
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
            got = str(max(hits, key=lambda f: f.stat().st_mtime))
            return sync_timing(video, got)
    return None


def sync_timing(video: str, srt_path: str, max_offset: int = 120) -> str:
    """Re-time an out-of-sync subtitle against the video's own audio.

    Uses ffsubsync (voice-activity detection — language-agnostic, so a Chinese
    soundtrack syncs an English sub fine), with alass as a fallback when its binary is
    present (alass handles non-linear drift: different cuts, ad breaks). Only meant for
    subs that did NOT ship with the release — release embedded/sidecar tracks are already
    synced, so callers skip this for them. Fail-safe: returns the ORIGINAL path on any
    error or if auto-sync is disabled, so the pipeline never breaks on a sync hiccup.
    """
    if not CFG.subs_autosync:
        return srt_path
    src = Path(srt_path)
    out = src.with_name(src.stem + ".synced" + src.suffix)
    # ffsubsync shells out to a bare `ffmpeg`; our ffmpeg is jellyfin's at a custom
    # path, so put its directory on PATH (FFMPEG_PATH alone isn't honored here).
    ffdir = str(Path(CFG.ffmpeg).parent)
    env = dict(os.environ, FFMPEG_PATH=ffdir,
               PATH=ffdir + os.pathsep + os.environ.get("PATH", ""))
    ffs = Path(sys.executable).with_name("ffsubsync")
    try:
        r = subprocess.run([str(ffs), video, "-i", str(src), "-o", str(out),
                            "--max-offset-seconds", str(max_offset)],
                           capture_output=True, text=True, env=env, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return srt_path
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return str(out)
    alass = shutil.which("alass") or shutil.which("alass-cli")
    if alass:
        try:
            r2 = subprocess.run([alass, video, str(src), str(out)],
                                capture_output=True, text=True, timeout=900, check=False)
            if r2.returncode == 0 and out.exists() and out.stat().st_size > 0:
                return str(out)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return srt_path  # fail-safe: keep original timing


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

def write_sidecar(video: str, ass_path: str, lang: str = "en") -> str:
    """Install a styled .ass as the video's default sidecar track (replacing an older
    one). The video is not touched. Returns the sidecar path."""
    dest = styled_sidecar(video, lang)
    os.replace(ass_path, dest)
    return str(dest)


def cleanup_online(sub_path: str) -> None:
    """Drop the intermediates of an online fetch (the downloaded .srt and its
    .synced re-timing) once the styled sidecar exists, so they don't show up as
    extra tracks."""
    p = Path(sub_path)
    orig = p.with_name(p.name.replace(".synced", "")) if ".synced" in p.name else p
    for f in {p, orig}:
        f.unlink(missing_ok=True)


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
            fonts = str(FONT_DIR).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            cmd = ["nice", "-n", "19", CFG.ffmpeg, "-y", "-i", video,
                   "-vf", f"ass='{esc}':fontsdir='{fonts}'", "-c:v", "libx264", "-preset", "veryfast",
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
