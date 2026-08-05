"""
SYSFOX :: admin commands

  /status      - health summary for each loop + uptime + guild count
  /testnotify  - drops a sample notification embed in the current channel (no ping)
  /postpanel   - (re)posts the role opt-in panel to the configured panel channel

All three require the Manage Server permission.
"""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

import branding
from config import Config
from feeds import Clip, Stream, Video
from health import HealthTracker
from cogs.panel import PanelCog, RolePanel


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config, tracker: HealthTracker) -> None:
        self.bot = bot
        self.config = config
        self.tracker = tracker

    @app_commands.command(name="status", description="Show SYSFOX health and uptime.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        snap = self.tracker.snapshot()
        color = branding.YELLOW if snap["status"] == "degraded" else branding.LIME
        embed = branding.dialog(
            "System Status",
            f"**Overall:** `{snap['status'].upper()}`\n"
            f"**Uptime:** {snap['uptime_seconds'] // 3600}h "
            f"{(snap['uptime_seconds'] % 3600) // 60}m\n"
            f"**Guilds:** {snap['guilds']}",
            color=color,
        )
        for name, lh in snap["loops"].items():
            state = "🟢 ok" if not lh["degraded"] else "🔴 degraded"
            since = lh["seconds_since_ok"]
            since_txt = f"{since}s ago" if since is not None else "never"
            embed.add_field(
                name=name,
                value=(
                    f"{state}\nlast ok: {since_txt}\n"
                    f"fails (streak/total): {lh['consecutive_failures']}/{lh['total_failures']}"
                ),
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="testnotify", description="Preview a sample notification here.")
    @app_commands.describe(kind="Which notification style to preview.")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Go-Live", value="live"),
            app_commands.Choice(name="Clip", value="clip"),
            app_commands.Choice(name="Video", value="video"),
            app_commands.Choice(name="Short", value="short"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def testnotify(
        self, interaction: discord.Interaction, kind: app_commands.Choice[str]
    ) -> None:
        notif = self.bot.get_cog("NotificationsCog")
        if notif is None:
            await interaction.response.send_message(
                "Notifications system isn't loaded.", ephemeral=True
            )
            return

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if kind.value == "live":
            embed = notif.live_embed(
                Stream(
                    id="test",
                    title="[TEST] Satisfactory — factory expansion night",
                    game_name="Satisfactory",
                    started_at=now,
                    thumbnail_url="",
                    login=self.config.twitch_login,
                )
            )
        elif kind.value == "clip":
            embed = notif.clip_embed(
                Clip(
                    id="test",
                    title="[TEST] Perfectly timed conveyor disaster",
                    url="https://twitch.tv/",
                    thumbnail_url="",
                    creator_name="a viewer",
                    created_at=now,
                )
            )
        else:
            embed = notif.video_embed(
                Video(
                    id="test",
                    title="[TEST] Sample upload",
                    url="https://youtube.com/",
                    published=now,
                    thumbnail_url="",
                    is_short=(kind.value == "short"),
                )
            )

        await interaction.response.send_message(
            content="*(preview — no role was pinged)*", embed=embed, ephemeral=True
        )

    @app_commands.command(name="postpanel", description="(Re)post the role opt-in panel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def postpanel(self, interaction: discord.Interaction) -> None:
        panel_cog = self.bot.get_cog("PanelCog")
        channel = self.bot.get_channel(self.config.panel_channel_id)
        if not isinstance(panel_cog, PanelCog) or not isinstance(
            channel, discord.abc.Messageable
        ):
            await interaction.response.send_message(
                "Panel channel is misconfigured — check PANEL_CHANNEL_ID.", ephemeral=True
            )
            return
        await channel.send(embed=panel_cog.build_embed(), view=RolePanel(self.config))
        await interaction.response.send_message(
            f"Panel posted in <#{self.config.panel_channel_id}>.", ephemeral=True
        )

    async def _perm_error(self, interaction: discord.Interaction, error) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to use that."
        else:
            msg = f"Command error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def cog_load(self) -> None:
        # Route app-command errors for this cog's commands to a friendly handler.
        for cmd in (self.status, self.testnotify, self.postpanel):
            cmd.on_error = self._perm_error
