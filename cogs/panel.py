"""
SYSFOX :: role opt-in panel

A persistent (timeout=None) button panel. Each button toggles one opt-in ping
role on the member who clicks it. Because the View is registered in the bot's
setup_hook with stable custom_ids, the buttons keep working after a restart or a
git re-clone without needing to repost the panel.
"""

from __future__ import annotations

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


class RolePanel(discord.ui.View):
    def __init__(self, config: Config) -> None:
        super().__init__(timeout=None)
        self._config = config
        for suffix, label, emoji, attr in _ROLE_BUTTONS:
            self.add_item(_RoleButton(getattr(config, attr), suffix, label, emoji))


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
                "That role isn't configured correctly — poke Neo.", ephemeral=True
            )
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="SYSFOX opt-out via panel")
                msg = f"Removed **{role.name}** — you won't be pinged for these."
            else:
                await member.add_roles(role, reason="SYSFOX opt-in via panel")
                msg = f"Added **{role.name}** — you're on the list."
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
        embed = branding.dialog(
            "Notification Control Panel",
            "Click a button to toggle pings on or off. Toggle again to opt out.\n\n"
            "🔴 **Go-Live** — Twitch stream start\n"
            "✂️ **Clips** — new Twitch clips\n"
            "📼 **Videos** — new YouTube uploads\n"
            "⚡ **Shorts** — new YouTube Shorts",
            color=branding.NAVY,
        )
        return embed
