"""The Discord "UI layer" shared across homelab services.

Two transports talk to the same owner over the same bot:

* **REST-over-curl** (:class:`Discord`), stdlib-only — used by the notifier
  services (rain-notifier, news-notifier). Lightweight: send, plus poll for inbound.
* a **discord.py Gateway bot** (media-concierge) — event-driven, with attachments,
  typing indicators, etc.

:class:`Messenger` is the small *outbound* contract both share: deliver a line to
the owner's DM, or to a given channel. Services program against ``Messenger`` so
the transport is swappable. Inbound differs per transport — the REST client polls
(:meth:`Discord.fetch`); the Gateway bot receives events — so inbound is not part
of the shared contract.

media-concierge's discord.py client is intentionally *not* modified to go through this:
its inbound event loop isn't expressible as ``Messenger``, and rewriting the live
bot's delivery earns little. It can present ``Messenger`` via a small adapter in
its own package if that's ever wanted — ``Messenger`` is ``runtime_checkable`` and
structural, so any object with ``send_dm``/``send`` already satisfies it.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .config import require, shared_env
from .notify import fetch_channel_messages, open_dm_channel, send_channel_message


@runtime_checkable
class Messenger(Protocol):
    """Outbound Discord surface: deliver text to the owner or to a channel."""

    def send_dm(self, text: str) -> None: ...
    def send(self, channel_id: str, text: str) -> None: ...


class Discord:
    """REST (curl) Discord client for notifier services — stdlib only.

    Opens the owner DM channel lazily and caches it, so a resident poll loop does
    one channel lookup for its lifetime instead of one per message. Implements
    :class:`Messenger`.
    """

    def __init__(self, token: str, owner_id: str) -> None:
        self._token = token
        self._owner_id = owner_id
        self._dm_channel: str | None = None

    def __repr__(self) -> str:
        # Never put the token in a repr — these objects end up in tracebacks/logs.
        return f"Discord(owner_id={self._owner_id!r})"

    @classmethod
    def from_env(cls) -> Discord:
        """Build from the shared ``.env`` (DISCORD_BOT_TOKEN / OWNER_DISCORD_ID)."""
        env = shared_env()
        return cls(require(env, "DISCORD_BOT_TOKEN"), require(env, "OWNER_DISCORD_ID"))

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def dm_channel(self) -> str:
        """Owner DM channel id, opened once and cached."""
        if self._dm_channel is None:
            self._dm_channel = open_dm_channel(self._token, self._owner_id)
        return self._dm_channel

    def send_dm(self, text: str) -> None:
        send_channel_message(self._token, self.dm_channel(), text)

    def send(self, channel_id: str, text: str) -> None:
        send_channel_message(self._token, channel_id, text)

    def fetch(self, channel_id: str, after: str | None = None) -> list[dict]:
        """Inbound (REST-only): messages in ``channel_id``, oldest-first."""
        return fetch_channel_messages(self._token, channel_id, after)
