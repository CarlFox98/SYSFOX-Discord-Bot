"""
SYSFOX :: role opt-in panel

A persistent (timeout=None) button panel. Each button toggles one opt-in ping
role on the member who clicks it. Because the View is registered in the bot's
setup_hook with stable custom_ids, the buttons keep working after a restart or a
git re-clone without needing to repost the panel.
"""

from __future__ import annotations

import os

import discord
from discord.ext import commands

import branding
from config import Config

# (custom_id suffix, label, emoji, config attribute holding the role id)
_ROLE_BUTTONS = [
    ("live", "Go-Live pings", "🔴", "live_role_id"),
    ("clips", "Clip pings", "✂️", "clips_role_id"),
    ("videos", "Video pings", "📼", "videos_role_id"),
    ("shorts", "Short pings", "⚡", "shorts_role_id"),
]


def _giveaway_role_id() -> int:
    raw = os.environ.get("GIVEAWAY_ROLE_ID", "").strip()
    try:
        return int(raw) if raw else 1557446192923803698
    except ValueError:
        return 0


class RolePanel(discord.ui.View):
    def __init__(self, config: Config) -> None:
        super().__init__(timeout=None)
        self._config = config
        for suffix, label, emoji, attr in _ROLE_BUTTONS:
            self.add_item(_RoleButton(getattr(config, attr), suffix, label, emoji))
        gw_role = _giveaway_role_id()
        if gw_role:
            self.add_item(_RoleButton(gw_role, "giveaways", "Giveaway pings", "🎁"))


class _RoleButton(discord.ui.Button):
    def __init__(self, role_id: int, suffix: str, label: str, emoji: str) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"sysfox:role:{suffix}",
        )
        self._role_id = role_id

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This only works inside the server.", ephemeral=True
            )
            return
        role = guild.get_role(self._role_id)
        member = interaction.user
        if role is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "That role isn't set up right. Let an Admin know.", ephemeral=True
            )
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="SYSFOX opt-out via panel")
                msg = f"Removed **{role.name}**. You won't be pinged for these."
            else:
                await member.add_roles(role, reason="SYSFOX opt-in via panel")
                msg = f"Added **{role.name}**. You're on the list."
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't manage that role. My role needs to sit **above** it in "
                "Server Settings → Roles.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(msg, ephemeral=True)


class PanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config) -> None:
        self.bot = bot
        self.config = config

    def build_embed(self) -> discord.Embed:
        lines = [
            "Click a button to toggle pings on or off. Toggle again to opt out.",
            "",
            "🔴 **Go-Live**: Twitch stream start",
            "✂️ **Clips**: new Twitch clips",
            "📼 **Videos**: new YouTube uploads",
            "⚡ **Shorts**: new YouTube Shorts",
        ]
        if _giveaway_role_id():
            lines.append("🎁 **Giveaways**: a new giveaway goes up")
        return branding.dialog(
            "Notification Control Panel", "\n".join(lines), color=branding.NAVY
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bring the already-posted panel up to date (new buttons, new copy)
        so it never has to be reposted by hand. Runs once per process."""
        if getattr(self, "_refreshed", False):
            return
        self._refreshed = True
        channel = self.bot.get_channel(self.config.panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != self.bot.user.id or not msg.embeds:  # type: ignore[union-attr]
                    continue
                if "Notification Control Panel" in (msg.embeds[0].title or ""):
                    await msg.edit(embed=self.build_embed(), view=RolePanel(self.config))
                    break
        except discord.HTTPException:
            pass
