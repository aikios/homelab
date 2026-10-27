#!/usr/bin/env python3
"""Move this host from a service's old name/layout to its new one.

Called by deploy/install.sh before it installs a service's units; a no-op when
deploy/renames.yml has no entry for the service or the host is already migrated.

    migrate.py <new-service>

For each old service dir listed in renames.yml it:
  1. stops, disables and removes the old systemd units installed on this host,
  2. moves the files git does not track (.env, state/, per-host secrets) from
     services/<old>/ into services/<new>/ (optionally remapping sub-paths), never
     overwriting: a clash is reported and exits non-zero so a human can merge it,
  3. renames env keys in services/<new>/.env (keys only; values are never printed).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RENAMES = Path(__file__).resolve().parent / "renames.yml"
UNIT_DIR = Path("/etc/systemd/system")
CACHE_DIRS = {"__pycache__", ".ruff_cache", ".ty_cache", ".pytest_cache"}


def load(new: str) -> dict | None:
    data = yaml.safe_load(RENAMES.read_text()) or {}
    return data.get(new)


def rename_env_keys(text: str, keys: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite `OLD=` assignments to `NEW=`; returns (text, renamed keys)."""
    done = []
    for old, new in keys.items():
        pat = re.compile(rf"^(\s*(?:export\s+)?){re.escape(old)}(\s*=)", re.MULTILINE)
        text, n = pat.subn(rf"\g<1>{new}\g<2>", text)
        if n:
            done.append(old)
    return text, done


def remap(rel: Path, paths: dict[str, str]) -> Path:
    """Apply the first matching old-prefix -> new-prefix rule to a relative path."""
    for old, new in paths.items():
        if rel == Path(old) or Path(old) in rel.parents:
            return Path(new) / rel.relative_to(old)
    return rel


def leftovers(old_dir: Path) -> list[Path]:
    """Files under old_dir that git doesn't track (after the rename commit, that
    is everything still there), skipping tool caches."""
    tracked = set(subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", str(old_dir)],
        capture_output=True, check=True, text=True).stdout.split("\0"))
    out = []
    for p in sorted(old_dir.rglob("*")):
        if p.is_dir() or CACHE_DIRS & set(p.relative_to(old_dir).parts):
            continue
        if str(p.relative_to(REPO)) not in tracked:
            out.append(p.relative_to(old_dir))
    return out


def move_leftovers(old_dir: Path, new_dir: Path, paths: dict[str, str]) -> list[str]:
    """Move untracked files across; returns clashes (left in place)."""
    clashes = []
    for rel in leftovers(old_dir):
        dst = new_dir / remap(rel, paths)
        if dst.exists():
            clashes.append(f"{old_dir / rel} -> {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir / rel), str(dst))
        print(f"   moved {old_dir.name / rel} -> {dst.relative_to(REPO)}")
    if not clashes:
        for d in sorted(old_dir.rglob("*"), key=lambda p: -len(p.parts)):
            if d.is_dir() and d.name in CACHE_DIRS:
                shutil.rmtree(d)
        for d in sorted([*old_dir.rglob("*"), old_dir], key=lambda p: -len(p.parts)):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return clashes


def remove_units(units: list[str]) -> None:
    removed = False
    for unit in units:
        if not (UNIT_DIR / unit).exists():
            continue
        subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", unit],
                       capture_output=True, check=False)  # static units refuse disable
        subprocess.run(["sudo", "-n", "rm", "-f", str(UNIT_DIR / unit)], check=True)
        print(f"   removed old unit {unit}")
        removed = True
    if removed:
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=True)


def main(argv: list[str]) -> int:
    new = argv[0]
    spec = load(new)
    if not spec:
        return 0
    new_dir = REPO / "services" / new
    remove_units(spec.get("units") or [])
    clashes = []
    for old in spec.get("dirs") or []:
        old_dir = REPO / "services" / old
        if old_dir.is_dir():
            clashes += move_leftovers(old_dir, new_dir, spec.get("paths") or {})
    env = new_dir / ".env"
    if env.exists() and spec.get("env"):
        text, done = rename_env_keys(env.read_text(), spec["env"])
        if done:
            env.write_text(text)
            print(f"   renamed env keys: {', '.join(done)}")
    if clashes:
        sys.stderr.write("!! not overwritten, merge by hand:\n   " + "\n   ".join(clashes) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
