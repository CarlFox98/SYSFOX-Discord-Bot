"""
SYSFOX :: stream logistics

  /delay minutes [note]   - "stream's running late" post that pings @Live
  /cancelstream [reason]  - "no stream today" post that pings @Live

Both post in the go-live channel and ping only the Live role, so quick schedule
updates no longer need an @everyone. Manage Server is required, same as the
other SYSFOX commands.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import branding
from config import Config

log = logging.getLogger("sysfox")


class StreamToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config) -> None:
        self.bot = bot
        self.config = config

    async def _announce(
        self, interaction: discord.Interaction, embed: discord.Embed, ping: bool
    ) -> None:
        channel = self.bot.get_channel(self.config.live_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I can't find the go-live channel. Check `LIVE_CHANNEL_ID`.", ephemeral=True
            )
            return
        try:
            msg = await channel.send(
                content=f"<@&{self.config.live_role_id}>" if ping else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Couldn't post in <#{self.config.live_channel_id}>: `{exc.status}`. "
                "Check SYSFOX's permissions there.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Posted in <#{self.config.live_channel_id}>"
            f"{' and pinged Live' if ping else ''}: {msg.jump_url}",
            ephemeral=True,
        )

    @app_commands.command(name="delay", description="Tell the Live role the stream is running late.")
    @app_commands.describe(
        minutes="How late you'll be (5 to 240).",
        note="Optional extra line, like the reason.",
        ping="Ping the Live role (default yes).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delay(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 5, 240],
        note: Optional[app_commands.Range[str, 1, 200]] = None,
        ping: bool = True,
    ) -> None:
        start = int(time.time()) + minutes * 60
        text = (
            f"Stream's running about **{minutes} min** late.\n"
            f"New start: <t:{start}:t> (<t:{start}:R>)."
        )
        if note:
            text += f"\n\n{note}"
        embed = branding.dialog("⏰ Running Late", text, color=branding.YELLOW)
        await self._announce(interaction, embed, ping)

    @app_commands.command(name="cancelstream", description="Tell the Live role there's no stream today.")
    @app_commands.describe(
        reason="Optional reason or when the next stream is.",
        ping="Ping the Live role (default yes).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cancelstream(
        self,
        interaction: discord.Interaction,
        reason: Optional[app_commands.Range[str, 1, 200]] = None,
        ping: bool = True,
    ) -> None:
        text = "No stream today, sorry!"
        if reason:
            text += f"\n\n{reason}"
        embed = branding.dialog("🚫 Stream Cancelled", text, color=branding.SILVER)
        await self._announce(interaction, embed, ping)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to use that."
        else:
            log.warning("streamtools command failed: %r", error)
            msg = f"⚠️ {type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
