"""Central config.

Loaded from three env files, in priority order (first value wins, since
python-dotenv's load_dotenv defaults to override=False):
  1. the host-level shared creds (ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN,
     OWNER_DISCORD_ID), at HOMELAB_SHARED_ENV or ~/homelab/secrets/shared.env;
  2. this service's own .env (JELLYFIN/RADARR/… URLs, media paths, cost knobs);
  3. .arr-keys.env (radarr/sonarr/prowlarr API keys, written by setup).
The three key sets are disjoint, so the ordering only matters if a key is
duplicated — the shared file is the single source of truth for the shared creds.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # services/mediabot
SHARED_ENV = Path(
    os.environ.get("HOMELAB_SHARED_ENV", "~/homelab/secrets/shared.env")
).expanduser()
load_dotenv(SHARED_ENV)
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".arr-keys.env")


def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"missing required env var: {key}")
    return v


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Config:
    # --- LLM / Discord ---
    anthropic_api_key: str
    discord_bot_token: str
    owner_discord_id: int
    model: str
    max_tokens: int

    # --- service URLs / keys ---
    radarr_url: str
    radarr_key: str
    sonarr_url: str
    sonarr_key: str
    prowlarr_url: str
    prowlarr_key: str
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    jellyfin_url: str
    jellyfin_key: str
    opensubtitles_api_key: str

    # --- filesystem / tooling ---
    ffmpeg: str
    ffprobe: str
    media_movies: str
    media_shows: str
    media_downloads: str
    min_free_gb: int
    state_dir: Path
    log_dir: Path

    # --- cost meter ---
    cost_credit_start: float
    cost_warn_usd: float
    cost_warn_cooldown_h: float

    # --- subtitle house style (ASS; colours are &HAABBGGRR) ---
    subs_font: str
    subs_size: int
    subs_primary: str
    subs_outline: str
    subs_italic: bool
    subs_outline_w: str
    subs_margin_v: int
    subs_autosync: bool

    # --- debug ---
    debug_sock: bool  # expose a local unix socket to inject "DMs" without Discord


def load() -> Config:
    root = ROOT
    state_dir = root / "state"
    log_dir = root / "logs"
    state_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)
    return Config(
        anthropic_api_key=_req("ANTHROPIC_API_KEY"),
        discord_bot_token=_get("DISCORD_BOT_TOKEN"),
        owner_discord_id=int(_get("OWNER_DISCORD_ID", "0") or "0"),
        model=_get("MODEL", "claude-opus-5"),
        max_tokens=int(_get("MAX_TOKENS", "16000")),
        radarr_url=_get("RADARR_URL", "http://localhost:7878"),
        radarr_key=_req("radarr_API_KEY"),
        sonarr_url=_get("SONARR_URL", "http://localhost:8989"),
        sonarr_key=_req("sonarr_API_KEY"),
        prowlarr_url=_get("PROWLARR_URL", "http://localhost:9696"),
        prowlarr_key=_req("prowlarr_API_KEY"),
        qbit_url=_get("QBIT_URL", "http://localhost:8080"),
        qbit_user=_get("QBIT_USER", "admin"),
        qbit_pass=_req("QBIT_PASS"),
        jellyfin_url=_get("JELLYFIN_URL", "http://localhost:8096"),
        jellyfin_key=_get("JELLYFIN_API_KEY"),
        opensubtitles_api_key=_get("OPENSUBTITLES_API_KEY", ""),
        ffmpeg=_get("FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg"),
        ffprobe=_get("FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe"),
        media_movies=_get("MEDIA_MOVIES", "/media/JellyFin-Movies"),
        media_shows=_get("MEDIA_SHOWS", "/media/JellyFin-Shows"),
        media_downloads=_get("MEDIA_DOWNLOADS", "/media/downloads"),
        min_free_gb=int(_get("MIN_FREE_GB", "8")),
        state_dir=state_dir,
        log_dir=log_dir,
        cost_credit_start=float(_get("COST_CREDIT_START_USD", "0") or "0"),
        cost_warn_usd=float(_get("COST_WARN_USD", "3") or "3"),
        cost_warn_cooldown_h=float(_get("COST_WARN_COOLDOWN_H", "6") or "6"),
        subs_font=_get("SUBS_FONT", "TeX Gyre Heros"),  # Helvetica-metric clone, bundled
        subs_size=int(_get("SUBS_SIZE", "54")),
        subs_primary=_get("SUBS_PRIMARY", "&H0000FFFF"),   # yellow
        subs_outline=_get("SUBS_OUTLINE", "&H00000000"),   # black
        subs_italic=_get("SUBS_ITALIC", "1") not in ("0", "false", "False", ""),
        subs_outline_w=_get("SUBS_OUTLINE_W", "2.6"),
        subs_margin_v=int(_get("SUBS_MARGIN_V", "40")),
        subs_autosync=_get("SUBS_AUTOSYNC", "1") not in ("0", "false", "False", ""),
        debug_sock=_get("MEDIABOT_DEBUG_SOCK", "1") not in ("0", "false", "False", ""),
    )


CFG = load()
