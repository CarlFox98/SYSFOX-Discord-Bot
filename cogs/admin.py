"""
SYSFOX :: admin commands

  /status      - health summary for each loop + uptime + guild count
  /testnotify  - drops a sample notification embed in the current channel (no ping)
  /postpanel   - (re)posts the role opt-in panel to the configured panel channel
  /reboot      - restarts the bot process
  /ping        - latency check (bot response time + gateway ping)
  /config      - show loaded config values (tokens redacted)
  /state       - show state store stats (seen clips, videos, primed streams)
  /guilds      - list all guilds the bot is in with member counts

All require the Manage Server permission.
"""

from __future__ import annotations

import json
import os
import signal
import sys
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
                    title="[TEST] Satisfactory: factory expansion night",
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
            content="*(preview, no role was pinged)*", embed=embed, ephemeral=True
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
                "Panel channel is misconfigured. Check PANEL_CHANNEL_ID.", ephemeral=True
            )
            return
        await channel.send(embed=panel_cog.build_embed(), view=RolePanel(self.config))
        await interaction.response.send_message(
            f"Panel posted in <#{self.config.panel_channel_id}>.", ephemeral=True
        )

    @app_commands.command(name="reboot", description="Restart the bot process.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reboot(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "🔄 Restarting…", ephemeral=True
        )
        # Send SIGTERM to self so the process exits cleanly and the panel restarts it.
        os.kill(os.getpid(), signal.SIGTERM)

    @app_commands.command(name="ping", description="Check bot latency (response time + gateway ping).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ping(self, interaction: discord.Interaction) -> None:
        start = time.monotonic()
        await interaction.response.send_message("🏓 Pinging…", ephemeral=True)
        delta = time.monotonic() - start
        ws_latency = self.bot.latency
        embed = branding.dialog(
            "Bot Latency",
            f"**API response:** {delta * 1000:.0f} ms\n"
            f"**WebSocket (gateway):** {ws_latency * 1000:.0f} ms",
            color=branding.LIME,
        )
        await interaction.edit_original_response(content=None, embed=embed)

    @app_commands.command(name="config", description="Show loaded config values (tokens redacted).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def _cmd_config(self, interaction: discord.Interaction) -> None:
        c = self.config
        lines = [
            f"**Guild ID:** `{c.guild_id}`",
            f"**Twitch login:** `{c.twitch_login}`",
            f"**YouTube channel:** `{c.youtube_channel_id}`",
            f"**Health port:** `{c.health_port}`",
            f"**Poll intervals:** live {c.poll_interval_live}s / clips: {c.poll_interval_clips}s / YouTube: {c.poll_interval_youtube}s",
            f"**State path:** `{c.state_path}`",
            "",
            "**Tokens:** set ✅ (values hidden for security)",
        ]
        channels = [
            ("Live", c.live_channel_id),
            ("Clips", c.clips_channel_id),
            ("Videos", c.videos_channel_id),
            ("Shorts", c.shorts_channel_id),
            ("Panel", c.panel_channel_id),
            ("Alerts", c.alert_channel_id),
        ]
        roles = [
            ("Live", c.live_role_id),
            ("Clips", c.clips_role_id),
            ("Videos", c.videos_role_id),
            ("Shorts", c.shorts_role_id),
        ]
        for name, cid in channels:
            lines.append(f"**{name} channel:** `{cid}`")
        for name, rid in roles:
            lines.append(f"**{name} role:** `{rid}`")
        embed = branding.dialog("Configuration", "\n".join(lines), color=branding.TEAL)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="state", description="Show state store stats (seen clips, videos, primed streams).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def state(self, interaction: discord.Interaction) -> None:
        from state import State
        s = self.bot.state  # type: ignore[attr-defined]
        try:
            data = s._load()
        except Exception as exc:
            await interaction.response.send_message(
                f"Failed to read state file: {exc}", ephemeral=True
            )
            return
        total_seen = sum(s.seen.values()) if hasattr(s, "seen") else 0
        primed = len(s.primed) if hasattr(s, "primed") else 0
        embed = branding.dialog(
            "State Store",
            f"**Seen items:** {total_seen}\n"
            f"**Primed streams:** {primed}\n"
            f"**File:** `{self.config.state_path}`\n"
            f"**Raw JSON:** {len(json.dumps(data))} bytes",
            color=branding.TEAL,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="guilds", description="List all guilds the bot is in with member counts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def guilds(self, interaction: discord.Interaction) -> None:
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        total = sum(g.member_count or 0 for g in guilds)
        parts = [f"**{len(guilds)} guild(s), {total} total members**\n"]
        for i, g in enumerate(guilds):
            name = g.name if g.name else "(unknown)"
            parts.append(f"`{i+1}.` **{name}**: {g.member_count or '?'} members")
            if len(parts) > 20:
                parts.append("\n*(truncated, too many guilds)*")
                break
        text = "\n".join(parts)
        embed = branding.dialog("Guild List", text, color=branding.TEAL)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _perm_error(self, interaction: discord.Interaction, error) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to use that."
        else:
            msg = f"⚠️ {type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def cog_load(self) -> None:
        # Route app-command errors for this cog's commands to a friendly handler.
        self.status.on_error = self._perm_error
        self.testnotify.on_error = self._perm_error
        self.postpanel.on_error = self._perm_error
        self.reboot.on_error = self._perm_error
        self.ping.on_error = self._perm_error
        self._cmd_config.on_error = self._perm_error
        self.state.on_error = self._perm_error
        self.guilds.on_error = self._perm_error
