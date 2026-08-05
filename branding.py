"""
SYSFOX :: branding

Central home for the Neo's Den / Windows 98 aesthetic so copy and colors stay
consistent across every embed the bot produces.
"""

from __future__ import annotations

import discord

# Win98 desktop palette
TEAL = 0x008080        # classic desktop background
NAVY = 0x000080        # active title bar
SILVER = 0xC0C0C0      # window chrome
RED = 0xFF0000         # errors / alerts
LIME = 0x00FF00        # "online" green
YELLOW = 0xFFFF00      # warnings

FOOTER = "SYSFOX.EXE :: Neo's Den"


def dialog(title: str, description: str = "", color: int = TEAL) -> discord.Embed:
    """A base embed framed like a Win98 dialog window."""
    embed = discord.Embed(
        title=f"▓ {title}",
        description=description,
        color=color,
    )
    embed.set_footer(text=FOOTER)
    return embed


# Per-notification presentation. Keeping these together makes it easy to keep the
# voice consistent and to retheme everything in one place.
LIVE_TITLE = "🔴 NeoTheFox is LIVE"
CLIP_TITLE = "✂️ New Twitch Clip"
VIDEO_TITLE = "📼 New YouTube Video"
SHORT_TITLE = "⚡ New YouTube Short"

OFFLINE_NOTE = "── stream ended ──"
