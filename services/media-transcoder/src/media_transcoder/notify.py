# notify.py -- media-transcoder Discord DMs (runs on the media host).
#
#   notify_submitted(jobs)   called by submit: one batched DM per run
#   media-transcoder-notify  report every newly filed done/ or failed/ job
#                            (fired by media-transcoder-notify.path the moment the
#                            worker files a job, so there is no poll delay)
#
# Delivery goes through homelab-core's Discord helper (shared bot token from
# secrets/shared.env). Notifying is best-effort: a Discord outage never breaks
# submitting or filing a job. Already-reported job ids live in
# <queue>/notified.json; the first run seeds it with whatever is already filed
# so the historical backlog is not replayed.

import fcntl
import glob
import json
import os
import time

BASE = os.path.expanduser(os.environ.get("MEDIA_TRANSCODER_QUEUE_DIR", "~/reencode_farm"))
STATE = os.path.join(BASE, "notified.json")
MAX_LINES = 15  # per DM; the rest are summarised as "...and N more"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " notify: " + msg, flush=True)


def send(text):
    """DM the owner; False (and a log line) instead of raising."""
    try:
        from homelab_core import send_discord_dm
        send_discord_dm(text)
        return True
    except (Exception, SystemExit) as e:  # noqa: BLE001 — never break the farm over a DM
        log("discord send failed: " + str(e)[:200])
        return False


def title(path):
    return os.path.splitext(os.path.basename(path or "?"))[0]


def gb(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{round(n / 1e6)} MB"


def dur(secs):
    m, s = divmod(int(secs), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def bullets(lines):
    out = ["• " + x for x in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        out.append(f"…and {len(lines) - MAX_LINES} more")
    return "\n".join(out)


# ---------------- submitted ----------------

def submitted_line(job):
    bits = [x for x in (job.get("why"),
                        gb(job["source_bytes"]) if job.get("source_bytes") else None) if x]
    tail = (" — " + " · ".join(bits)) if bits else ""
    return f"**{title(job.get('source'))}**{tail}"


def notify_submitted(jobs):
    if not jobs:
        return
    head = f"🎞️ Queued {len(jobs)} file{'' if len(jobs) == 1 else 's'} for re-encoding:"
    send(head + "\n" + bullets([submitted_line(j) for j in jobs]))


# ---------------- results ----------------

def result_line(job):
    """(kind, text) for a filed job; kind is done / failed / skip."""
    r = job.get("result") or {}
    name = f"**{title(job.get('source'))}**"
    if r.get("status") == "failed":
        if r.get("replaced"):
            return "failed", name + " — replaced, but a later step failed: " + r.get("error", "?")
        return "failed", name + " — " + r.get("error", "?") + " (original untouched)"
    detail = r.get("detail", "")
    if detail == "already-friendly":
        return "skip", name + " — already direct-playable, nothing to do"
    bits = []
    if job.get("source_bytes") and r.get("out_bytes"):
        bits.append(f"{gb(job['source_bytes'])} → {gb(r['out_bytes'])} mp4")
    if r.get("mode"):
        bits.append(r["mode"])
    if r.get("secs"):
        bits.append(dur(r["secs"]))
    if r.get("subs"):
        bits.append(f"{r['subs']} sub{'' if r['subs'] == 1 else 's'}")
    if detail.startswith("uploaded"):
        bits.append("⚠️ " + detail)
    return "done", name + ((" — " + " · ".join(bits)) if bits else "")


def filed_jobs():
    out = []
    for state in ("done", "failed"):
        for fn in sorted(glob.glob(os.path.join(BASE, state, "*.json"))):
            try:
                with open(fn) as f:
                    job = json.load(f)
            except (ValueError, OSError):
                continue
            job.setdefault("id", os.path.basename(fn)[:-5])
            job.setdefault("result", {}).setdefault("status", state)
            out.append(job)
    return out


def load_state():
    try:
        with open(STATE) as f:
            return set(json.load(f))
    except (ValueError, OSError):
        return None


def save_state(ids):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, STATE)


def notify_results():
    # The path unit and the scan timer's backstop can both fire; serialise so
    # a job is never reported twice.
    with open(STATE + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _notify_results()


def _notify_results():
    jobs = filed_jobs()
    seen = load_state()
    if seen is None:  # first run: don't replay the backlog
        save_state({j["id"] for j in jobs})
        log(f"seeded with {len(jobs)} already-filed job(s)")
        return 0
    new = [j for j in jobs if j["id"] not in seen]
    if not new:
        return 0
    groups = {"done": [], "failed": [], "skip": []}
    for j in new:
        kind, line = result_line(j)
        groups[kind].append(line)
    msgs = []
    if groups["done"]:
        msgs.append("✅ Re-encoded and replaced:\n" + bullets(groups["done"]))
    if groups["failed"]:
        msgs.append("❌ Re-encode failed:\n" + bullets(groups["failed"]))
    if groups["skip"]:
        msgs.append("ℹ️ Skipped:\n" + bullets(groups["skip"]))
    # Only mark as reported once delivered, so a Discord outage retries next fire.
    if all(send(m) for m in msgs):
        save_state(seen | {j["id"] for j in new})
    log(f"reported {len(new)} job(s)")
    return len(new)


def main():
    notify_results()


if __name__ == "__main__":
    main()
