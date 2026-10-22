#!/usr/bin/env python3
# reencode_worker.py -- vega re-encode farm worker daemon.
# Polls the job queue on blacklodge (~/reencode_farm/queue) over ssh, claims one
# job at a time, re-encodes it via farm_core, and files the job as done/failed.
# Claim protocol (ssh mv = atomic) supports multiple workers / a future farm.

import fcntl
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farm_core as core

# Remote queue dir on the media host (relative to its home), matches farm_submit's
# FARM_QUEUE_DIR there. Not a secret — just a directory name.
QUEUE_DIR = os.environ.get("FARM_REMOTE_QUEUE", "reencode_farm")
LOCK = os.environ.get("FARM_LOCK", "/tmp/reencode-worker.lock")


def ssh(cmdstring):
    return subprocess.run(core.SSH + [cmdstring], check=False, capture_output=True, text=True)


def claim():
    r = ssh(
        f'for f in ~/{QUEUE_DIR}/queue/*.json; do [ -e "$f" ] || continue; '
        f'mv "$f" ~/{QUEUE_DIR}/claimed/ 2>/dev/null && {{ basename "$f"; break; }}; done')
    name = r.stdout.strip()
    if not name:
        return None, None
    r2 = ssh(f"cat ~/{QUEUE_DIR}/claimed/{name}")
    try:
        return name, json.loads(r2.stdout)
    except ValueError:
        return name, {"source": None, "_error": "unparseable job json"}


def move(name, dest):
    ssh(f"mv ~/{QUEUE_DIR}/claimed/{name} ~/{QUEUE_DIR}/{dest}")


def main():
    core.log(f"worker up (polling ~/{QUEUE_DIR}/queue on {core.HOST})")
    while True:
        try:
            with open(LOCK, "a+") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    time.sleep(30)  # another batch/worker holds the lock
                    continue
                name, job = claim()
                if not name:
                    time.sleep(15)
                    continue
                core.log("job " + name + ": " + (job.get("source") or "?"))
                try:
                    result = core.process_job(job)
                    move(name, "done")
                    core.log("job " + name + " DONE: " + result)
                except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
                    try:
                        move(name, "failed")
                    except Exception as move_err:  # noqa: BLE001 — best-effort filing
                        core.log("could not file job " + name + " as failed: " + str(move_err)[:150])
                    core.log("job " + name + " FAILED: " + str(e)[:300])
        except Exception as e:  # noqa: BLE001 — the daemon loop never exits on error
            core.log("worker loop error: " + str(e)[:200])
            time.sleep(30)


if __name__ == "__main__":
    main()