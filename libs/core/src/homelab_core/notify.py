"""Discord delivery for homelab services.

Shared by the notifier services (rain-notifier, media-transcoder's notify):

- the bot token only ever lives in this process (an in-memory request header),
  never on argv or in an env var that shows up in ``ps`` / ``systemctl status``;
- requests reuse one keep-alive HTTPS connection per thread (a fresh ``curl`` +
  TLS handshake per request measured 180 ms CPU vs 4.6 ms on a reused connection,
  2026-10-27, back when a 2 s DM poller ran on it);
- requests carry an explicit ``User-Agent`` because Discord's REST API sits
  behind Cloudflare and blocks bare clients with HTTP 403 ``error code: 1010``.

All services historically share one bot token (read live from the shared
``.env`` via :func:`homelab_core.config.shared_env`) so DMs arrive from the same
bot the owner already talks to.

One-shot DMs use :func:`send_discord_dm`; :class:`homelab_core.ui.Discord` keeps
the DM channel open for a long-lived sender (:func:`open_dm_channel`,
:func:`send_channel_message`). Reading the channel back is media-concierge's job
(discord.py Gateway); nothing here polls it.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from typing import Any

from .config import require, shared_env

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/homelab/homelab, 0.1)"
MAX_MESSAGE_CHARS = 2000  # Discord's hard limit on message content
MAX_RETRY_AFTER = 10.0    # s; longer rate-limit waits surface as an error instead

_local = threading.local()  # one keep-alive connection per thread


def _connection(host: str) -> http.client.HTTPSConnection:
    conn = getattr(_local, "conn", None)
    if conn is None or conn.host != host:
        conn = _local.conn = http.client.HTTPSConnection(host, timeout=20)
    return conn


def _request(method: str, url: str, token: str, payload: dict | None = None) -> Any:
    parts = urllib.parse.urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    raw = b""
    for attempt in range(3):
        try:
            conn = _connection(parts.netloc)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.HTTPException, OSError) as e:
            # an idle keep-alive socket the server already closed: reconnect
            stale = getattr(_local, "conn", None)
            if stale is not None:
                stale.close()
            _local.conn = None
            if attempt == 2:
                raise RuntimeError(f"Discord request failed: {e}") from e
            continue
        if resp.status == 429 and attempt < 2:
            wait = float((json.loads(raw or b"{}") or {}).get("retry_after", 1))
            if wait > MAX_RETRY_AFTER:
                raise RuntimeError(f"Discord rate limited for {wait:.0f}s")
            time.sleep(wait)
            continue
        break
    raw = raw.strip()
    data = json.loads(raw) if raw else None
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


def send_discord_dm(message: str, *, token: str | None = None,
                    owner_id: str | None = None) -> dict:
    """DM ``message`` to the owner. Opens (or reuses) the DM channel, then posts.

    ``token`` / ``owner_id`` default to ``DISCORD_BOT_TOKEN`` / ``OWNER_DISCORD_ID``
    from the shared ``.env``.
    """
    token, owner_id = _creds(token, owner_id)
    return send_channel_message(token, open_dm_channel(token, owner_id), message)
