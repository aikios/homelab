"""Discord delivery for homelab services.

Consolidated from the news-notifier / rain-notifier pattern:

- the bot token goes to ``curl`` via a stdin config (``-K -``), never on argv or
  an env var that shows up in ``ps`` / ``systemctl status``;
- requests carry an explicit ``User-Agent`` because Discord's REST API sits
  behind Cloudflare and blocks bare clients with HTTP 403 ``error code: 1010``.

All services historically share one bot token (read live from the shared
``.env`` via :func:`homelab_core.config.shared_env`) so DMs arrive from the same
bot the owner already talks to.

Both services' private ``_discord()`` helpers are reconciled here: rain-notifier
only ever sends a one-shot DM (:func:`send_discord_dm`), while news-notifier also
needs to open the DM channel once and reuse it, send to a known channel, and
*read* the channel back for owner commands — hence the channel-level primitives
(:func:`open_dm_channel`, :func:`send_channel_message`,
:func:`fetch_channel_messages`). Only the token is sensitive, so only it goes
through the ``-K -`` stdin config; method/url/body are fine on argv.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import require, shared_env

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/homelab/homelab, 0.1)"
MAX_MESSAGE_CHARS = 2000  # Discord's hard limit on message content


def _request(method: str, url: str, token: str, payload: dict | None = None) -> Any:
    args = ["curl", "-sS", "-X", method, "-H", f"User-Agent: {USER_AGENT}"]
    if payload is not None:
        args += ["-H", "Content-Type: application/json", "--data", json.dumps(payload)]
    args += ["-K", "-", url]
    proc = subprocess.run(
        args, check=False, input=f'header = "Authorization: Bot {token}"\n',
        text=True, capture_output=True, timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}): {proc.stderr.strip()}")
    body = proc.stdout.strip()
    data = json.loads(body) if body else None
    if isinstance(data, dict) and data.get("code") and "message" in data:
        raise RuntimeError(f"Discord API error {data['code']}: {data['message']}")
    return data


def _creds(token: str | None, owner_id: str | None) -> tuple[str, str]:
    """Fill in ``token`` / ``owner_id`` from the shared ``.env`` when not given."""
    env = shared_env()
    return (token or require(env, "DISCORD_BOT_TOKEN"),
            owner_id or require(env, "OWNER_DISCORD_ID"))


def open_dm_channel(token: str, owner_id: str) -> str:
    """Open (or reuse) the owner's DM channel and return its id."""
    channel = _request("POST", f"{API}/users/@me/channels", token,
                        {"recipient_id": owner_id})
    return channel["id"]


def send_channel_message(token: str, channel_id: str, content: str) -> dict:
    """Post ``content`` to a known channel, truncated to Discord's 2000-char limit."""
    return _request("POST", f"{API}/channels/{channel_id}/messages", token,
                    {"content": content[:MAX_MESSAGE_CHARS]})


def fetch_channel_messages(token: str, channel_id: str, after: str | None = None,
                           limit: int = 100) -> list[dict]:
    """Return messages in a channel, oldest-first.

    Discord returns newest-first; callers paging a DM for new owner commands want
    chronological order, so sort by the snowflake id here. ``after`` limits the
    result to messages newer than that id.
    """
    path = f"{API}/channels/{channel_id}/messages?limit={limit}"
    if after:
        path += f"&after={after}"
    data = _request("GET", path, token)
    messages = data if isinstance(data, list) else []
    return sorted(messages, key=lambda m: int(m["id"]))


def send_discord_dm(message: str, *, token: str | None = None,
                    owner_id: str | None = None) -> dict:
    """DM ``message`` to the owner. Opens (or reuses) the DM channel, then posts.

    ``token`` / ``owner_id`` default to ``DISCORD_BOT_TOKEN`` / ``OWNER_DISCORD_ID``
    from the shared ``.env``.
    """
    token, owner_id = _creds(token, owner_id)
    return send_channel_message(token, open_dm_channel(token, owner_id), message)
