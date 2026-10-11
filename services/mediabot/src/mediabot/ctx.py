"""Per-request context (which Discord channel the current agent turn belongs to)."""
from __future__ import annotations

from contextvars import ContextVar

_channel: ContextVar[int] = ContextVar("mediabot_channel", default=0)


def set_channel(cid: int) -> None:
    _channel.set(cid)


def channel_id() -> int:
    return _channel.get()
