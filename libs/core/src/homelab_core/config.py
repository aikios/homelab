"""Environment / secret loading shared by homelab services.

Secrets live in per-host ``.env`` files that are never committed. Several
services reuse one shared ``.env`` (historically mediabot's) for the Discord bot
credentials; point ``HOMELAB_SHARED_ENV`` at it, or pass a path explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` .env file into a dict.

    Ignores blank lines and ``#`` comments, strips surrounding quotes, and does
    not mutate ``os.environ``. A missing file yields an empty dict.
    """
    env: dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.exists():
        return env
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


DEFAULT_SHARED_ENV = "~/homelab/secrets/shared.env"


def shared_env() -> dict[str, str]:
    """Load the host-level shared credentials file.

    Holds the keys more than one service needs (the Discord bot token + owner id,
    the Anthropic key). Path comes from ``HOMELAB_SHARED_ENV`` if set, else
    ``~/homelab/secrets/shared.env``. Per-service keys live in that service's own
    ``.env``, not here.
    """
    return load_env(os.environ.get("HOMELAB_SHARED_ENV", DEFAULT_SHARED_ENV))


def require(env: dict[str, str], key: str) -> str:
    """Return ``env[key]`` or exit with a clear message if it is missing."""
    try:
        return env[key]
    except KeyError:
        raise SystemExit(f"missing required config: {key}") from None
