"""
SYSFOX :: configuration loader

All secrets and IDs come from environment variables. On bot-hosting.net these are
set in the panel's Startup tab (Environment Variables) so they persist across
re-installs and never live in the git repo. For local development you can drop a
.env file next to this module (see .env.example) and it will be loaded automatically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Optional: load a local .env for development. In production (bot-hosting.net) the
# variables are already in the environment, so a missing .env is not an error.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"Missing required environment variable: {name}")
    return val


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, *, required: bool = False, default: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- Discord core ---
    discord_token: str
    guild_id: int

    # --- Twitch (Helix API, client-credentials app token) ---
    twitch_client_id: str
    twitch_client_secret: str
    twitch_login: str  # e.g. "NeoTheFox98"

    # --- YouTube (RSS feed, no API key required) ---
    youtube_channel_id: str  # e.g. "UCIJQfv1gmlQrdtmIyq170_g"

    # --- Destination channels ---
    live_channel_id: int
    clips_channel_id: int
    videos_channel_id: int
    shorts_channel_id: int
    panel_channel_id: int
    alert_channel_id: int  # where health degradation alerts are posted

    # --- Opt-in ping roles ---
    live_role_id: int
    clips_role_id: int
    videos_role_id: int
    shorts_role_id: int

    # --- Operational tuning ---
    health_port: int = 8080
    poll_interval_live: int = 60      # seconds
    poll_interval_clips: int = 300    # seconds
    poll_interval_youtube: int = 300  # seconds
    state_path: str = "data/state.json"

    # Consecutive-failure thresholds before a loop is flagged "degraded".
    # YouTube's RSS feed 404s intermittently from datacenter IPs, so it gets a
    # more forgiving threshold (~30 min at a 5-min poll) than Twitch.
    fail_threshold_twitch: int = 3
    fail_threshold_clips: int = 3
    fail_threshold_youtube: int = 6

    @property
    def youtube_feed_url(self) -> str:
        return (
            "https://www.youtube.com/feeds/videos.xml?channel_id="
            f"{self.youtube_channel_id}"
        )

    @classmethod
    def load(cls) -> "Config":
        return cls(
            discord_token=_req("DISCORD_TOKEN"),
            guild_id=_int("GUILD_ID", required=True),
            twitch_client_id=_req("TWITCH_CLIENT_ID"),
            twitch_client_secret=_req("TWITCH_CLIENT_SECRET"),
            twitch_login=_req("TWITCH_LOGIN"),
            youtube_channel_id=_req("YOUTUBE_CHANNEL_ID"),
            live_channel_id=_int("LIVE_CHANNEL_ID", required=True),
            clips_channel_id=_int("CLIPS_CHANNEL_ID", required=True),
            videos_channel_id=_int("VIDEOS_CHANNEL_ID", required=True),
            shorts_channel_id=_int("SHORTS_CHANNEL_ID", required=True),
            panel_channel_id=_int("PANEL_CHANNEL_ID", required=True),
            alert_channel_id=_int("ALERT_CHANNEL_ID", required=True),
            live_role_id=_int("LIVE_ROLE_ID", required=True),
            clips_role_id=_int("CLIPS_ROLE_ID", required=True),
            videos_role_id=_int("VIDEOS_ROLE_ID", required=True),
            shorts_role_id=_int("SHORTS_ROLE_ID", required=True),
            health_port=_int("HEALTH_PORT", default=8080),
            poll_interval_live=_int("POLL_INTERVAL_LIVE", default=60),
            poll_interval_clips=_int("POLL_INTERVAL_CLIPS", default=300),
            poll_interval_youtube=_int("POLL_INTERVAL_YOUTUBE", default=300),
            state_path=_opt("STATE_PATH", "data/state.json"),
        )
