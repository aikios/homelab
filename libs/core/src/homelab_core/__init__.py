"""homelab-core: shared building blocks for homelab services.

Extracted from the owner-built services on the server so the Discord-notify,
env/secret, and Home-Assistant-location patterns are written once instead of
copy-pasted per service.
"""
from .config import load_env, require, shared_env
from .ha import home_location
from .notify import (
    fetch_channel_messages,
    open_dm_channel,
    send_channel_message,
    send_discord_dm,
)
from .ui import Discord, Messenger

__version__ = "0.1.0"
__all__ = [
    "Discord",
    "Messenger",
    "fetch_channel_messages",
    "home_location",
    "load_env",
    "open_dm_channel",
    "require",
    "send_channel_message",
    "send_discord_dm",
    "shared_env",
]
