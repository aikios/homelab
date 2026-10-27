#!/usr/bin/env python3
# farm_core.py -- shared re-encode logic for the vega re-encode farm.
# Worker pulls a source over ssh from blacklodge, re-encodes to mp4/h264 via
# VAAPI (Intel UHD), extracts text subs as styled sidecars, uploads, verifies,
# swaps in the new file and (optionally) refreshes Jellyfin.

import json
import os
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

# Config comes from the environment (the systemd unit's EnvironmentFile); no real
# host, key, or path lives in this public tree. See .env.example.
HOST = os.environ.get("FARM_SSH_HOST", "")  # ssh target holding the media, e.g. user@server
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST]
REMOTE_FFPROBE = os.environ.get("REMOTE_FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe")
JF = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
STAGE = os.environ.get("FARM_STAGE", "/tmp/reencode-farm")
os.makedirs(STAGE, exist_ok=True)
# Runtime log under the service's gitignored state/ dir (service_dir/state/).
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LOG = os.environ.get("FARM_LOG") or str(STATE_DIR / "reencode_worker.log")
TEXT_SUB_CODECS = ("ass", "ssa", "srt", "subrip")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kw)


def ssh(cmdstring):
    return run(SSH + [cmdstring])


def pipe_ffmpeg(args, path):
    src = subprocess.Popen(SSH + ["cat " + shlex.quote(path)], stdout=subprocess.PIPE)
    ff = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + args,
        stdin=src.stdout, check=False, capture_output=True, text=True)
    assert src.stdout is not None  # stdout=PIPE always sets it
    src.stdout.close()
    src.wait()
    return ff


def ffprobe_remote(path):
    r = ssh(shlex.quote(REMOTE_FFPROBE) +
            " -v error -show_streams -show_entries format=duration,format_name -of json " +
            shlex.quote(path))
    if r.returncode != 0:
        raise RuntimeError("remote ffprobe failed: " + (r.stderr or "").strip()[-200:])
    return json.loads(r.stdout)


def pick_audio(streams):
    for s in streams:
        if s.get("codec_type") == "audio" and s.get("disposition", {}).get("default"):
            return s
    for s in streams:
        if s.get("codec_type") == "audio":
            return s
    raise RuntimeError("no audio stream found")


def text_subs(streams):
    return [s for s in streams
            if s.get("codec_type") == "subtitle" and
            (s.get("codec_name") or "").lower() in TEXT_SUB_CODECS]


def is_friendly(fmt, streams):
    if "mp4" not in (fmt.get("format_name") or "").split(","):
        return False
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return v.get("codec_name") == "h264" and a.get("codec_name") == "aac"


def encode(source, streams, local_mp4):
    a = pick_audio(streams)
    aidx = str(a.get("index", 1))
    ac = ["-c:a", "copy"] if a.get("codec_name") == "aac" else ["-c:a", "aac", "-b:a", "256k"]
    attempts = [
        # A: NVIDIA full-GPU (NVDEC decode + NVENC encode) — handles 10-bit HEVC
        ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", "pipe:0",
         "-map", "0:V:0", "-map", "0:" + aidx, "-vf", "scale_cuda=format=nv12",
         "-c:v", "h264_nvenc", "-rc", "constqp", "-qp", "24"] + ac +
        ["-sn", "-movflags", "+faststart", local_mp4],
        # B: GPU decode + GPU encode (iGPU VAAPI)
        ["-vaapi_device", "/dev/dri/renderD128", "-hwaccel", "vaapi",
         "-hwaccel_output_format", "vaapi", "-i", "pipe:0",
         "-map", "0:V:0", "-map", "0:" + aidx, "-vf", "scale_vaapi=format=nv12",
         "-c:v", "h264_vaapi", "-qp", "24", "-b:v", "0"] + ac +
        ["-sn", "-movflags", "+faststart", local_mp4],
        # C: CPU decode + GPU encode (fallback: av1, odd profiles, 10-bit hevc)
        ["-vaapi_device", "/dev/dri/renderD128", "-i", "pipe:0",
         "-map", "0:V:0", "-map", "0:" + aidx, "-vf", "format=nv12,hwupload",
         "-c:v", "h264_vaapi", "-qp", "24", "-b:v", "0"] + ac +
        ["-sn", "-movflags", "+faststart", local_mp4],
    ]
    labels = ["NVIDIA", "GPU-decode", "CPU-decode"]
    for n, args in enumerate(attempts):
        ff = pipe_ffmpeg(args, source)
        if ff.returncode == 0 and os.path.exists(local_mp4) and os.path.getsize(local_mp4) > 1_000_000:
            return labels[n]
        if os.path.exists(local_mp4):
            os.remove(local_mp4)
        log(labels[n] + " attempt failed: " + (ff.stderr or "").strip()[-200:])
    return None


def extract_subs(source, streams, base):
    sidecars = []
    for s in text_subs(streams):
        lang = "und"
        for tag in (s.get("tags") or {}).values():
            if isinstance(tag, str) and 2 <= len(tag) <= 3 and tag.isalpha():
                lang = tag.lower()
                break
        codec = (s.get("codec_name") or "").lower()
        ext = "ass" if codec in ("ass", "ssa") else "srt"
        local_sub = os.path.join(STAGE, base + "." + lang + "." + ext)
        ff = pipe_ffmpeg(["-i", "pipe:0", "-map", "0:" + str(s["index"]),
                          "-c:s", "copy", local_sub], source)
        if ff.returncode == 0 and os.path.exists(local_sub) and os.path.getsize(local_sub) > 100:
            sidecars.append((local_sub, base + "." + lang + "." + ext))
        else:
            log("sub extract failed idx " + str(s.get("index")) + ": " +
                (ff.stderr or "").strip()[-200:])
    return sidecars


def verify_output(local_mp4, src_dur):
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", local_mp4])
    odur = float(pr.stdout.strip() or 0)
    if src_dur and odur < src_dur * 0.85:
        raise RuntimeError(f"duration mismatch: out {odur:.1f}s vs source {src_dur:.1f}s")
    pr2 = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=codec_name", "-of", "csv=p=0", local_mp4])
    if pr2.stdout.strip() != "h264":
        raise RuntimeError("output codec is " + pr2.stdout.strip() + ", expected h264")


def refresh_library():
    req = urllib.request.Request(JF + "/Library/Refresh?api_key=" + API_KEY,
                                 method="POST", data=b"")
    urllib.request.urlopen(req, timeout=30)


def process_job(job):
    """Returns a short result string; raises on failure. Original is only
    removed after the replacement is uploaded and byte-verified. Details for
    the owner notification accumulate in job["result_info"] as steps finish
    (so a failure after the swap still reports replaced=True)."""
    info = job.setdefault("result_info", {})
    source = job["source"]
    if not source or not isinstance(source, str):
        raise RuntimeError("bad job: missing source")
    stem = os.path.splitext(source)[0]
    base = os.path.basename(stem)
    outdir = os.path.dirname(source)
    local_mp4 = os.path.join(STAGE, base + ".mp4")
    remote_mp4 = outdir + "/" + base + ".mp4"
    t0 = time.time()

    probe = ffprobe_remote(source)
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    src_dur = float(fmt.get("duration") or 0)

    if is_friendly(fmt, streams):
        return "already-friendly"

    mode = encode(source, streams, local_mp4)
    if not mode:
        raise RuntimeError("encode failed (both modes)")
    info.update(mode=mode, out_bytes=os.path.getsize(local_mp4))
    log("encode ok via " + mode + " (" + str(int(time.time() - t0)) + "s, " +
        str(round(os.path.getsize(local_mp4) / 1e6)) + "MB)")

    verify_output(local_mp4, src_dur)

    sidecars = []
    if job.get("extract_subs", True):
        sidecars = extract_subs(source, streams, base)
        for _, rname in sidecars:
            log("sub sidecar: " + rname)
    info["subs"] = len(sidecars)

    # upload (scp SFTP mode: raw paths, no shell quoting)
    up = run(["scp", "-o", "BatchMode=yes", local_mp4, HOST + ":" + remote_mp4])
    if up.returncode != 0:
        raise RuntimeError("scp upload failed: " + up.stderr.strip()[-200:])
    for lf, rname in sidecars:
        run(["scp", "-o", "BatchMode=yes", lf, HOST + ":" + outdir + "/" + rname])

    chk = ssh("stat -c %s " + shlex.quote(remote_mp4))
    if chk.stdout.strip() != str(os.path.getsize(local_mp4)):
        raise RuntimeError("remote size mismatch; original kept, new file left alongside")

    for f in [local_mp4] + [lf for lf, _ in sidecars]:
        try:
            os.remove(f)
        except OSError:
            pass

    if job.get("replace", True):
        rm = ssh("rm " + shlex.quote(source))
        if rm.returncode != 0:
            return "uploaded (original rm failed: " + rm.stderr.strip()[-150:] + ")"
        info["replaced"] = True
    info["output"] = remote_mp4
    info["secs"] = int(time.time() - t0)

    if job.get("refresh", True):
        refresh_library()

    return "ok via " + mode + " (" + str(int(time.time() - t0)) + "s)"