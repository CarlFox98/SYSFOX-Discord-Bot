"""
SYSFOX :: notification loops

Four background loops:
  * live    - Twitch go-live (and stream-ended message editing)
  * clips   - new Twitch clips
  * youtube - new YouTube uploads, routed to the Videos or Shorts channel

Each loop reports success/failure to the HealthTracker. When a loop crosses its
consecutive-failure threshold it posts one degradation alert to the alert channel,
and posts a recovery note when it comes back. YouTube uses a more forgiving
threshold because its RSS feed 404s intermittently from datacenter IPs.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

import branding
from config import Config
from feeds import Clip, Stream, TwitchClient, Video, YouTubeClient
from health import HealthTracker
from state import State

log = logging.getLogger("sysfox")


class NotificationsCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: Config,
        state: State,
        tracker: HealthTracker,
        session,
    ) -> None:
        self.bot = bot
        self.config = config
        self.state = state
        self.tracker = tracker
        self.session = session
        self._post_alerted: dict[int, float] = {}
        self._last_warning: dict[str, float] = {}
        self._warned: dict[str, bool] = {}

        self.twitch = TwitchClient(
            config.twitch_client_id, config.twitch_client_secret, config.twitch_login
        )
        self.youtube = YouTubeClient(config.youtube_feed_url)

        self.h_live = tracker.register("twitch_live", config.fail_threshold_twitch)
        self.h_clips = tracker.register("twitch_clips", config.fail_threshold_clips)
        self.h_youtube = tracker.register("youtube", config.fail_threshold_youtube)

        self.live_loop.change_interval(seconds=config.poll_interval_live)
        self.clips_loop.change_interval(seconds=config.poll_interval_clips)
        self.youtube_loop.change_interval(seconds=config.poll_interval_youtube)

    def cog_unload(self) -> None:
        self.live_loop.cancel()
        self.clips_loop.cancel()
        self.youtube_loop.cancel()

    async def cog_load(self) -> None:
        self.live_loop.start()
        self.clips_loop.start()
        self.youtube_loop.start()

    # --- shared helpers -----------------------------------------------------
    def _channel(self, channel_id: int) -> discord.abc.Messageable | None:
        ch = self.bot.get_channel(channel_id)
        return ch if isinstance(ch, discord.abc.Messageable) else None

    async def _post(self, channel_id: int, role_id: int, embed: discord.Embed) -> discord.Message | None:
        """Post a ping + embed. Returns None (never raises) if it can't, so a
        permissions problem never kills a loop. Callers treat None as
        'not posted yet' and retry on the next tick."""
        channel = self._channel(channel_id)
        if channel is None:
            log.warning("Notification channel %s not found", channel_id)
            return None
        try:
            return await channel.send(
                content=f"<@&{role_id}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
        except discord.HTTPException as exc:
            log.warning("Could not post in channel %s: %s", channel_id, exc)
            await self._post_failure_alert(channel_id, exc)
            return None

    async def _post_failure_alert(self, channel_id: int, exc: discord.HTTPException) -> None:
        """At most one alert per channel per hour."""
        now = time.monotonic()
        if now - self._post_alerted.get(channel_id, -1e9) < 3600:
            return
        self._post_alerted[channel_id] = now
        await self._safe_alert(
            branding.dialog(
                "⚠ Post Failed",
                f"Couldn't post in <#{channel_id}>: `{exc.status} {str(exc.text)[:200]}`\n"
                "Check SYSFOX's permissions in that channel. I'll keep retrying.",
                color=branding.YELLOW,
            )
        )

    async def _safe_alert(self, embed: discord.Embed) -> None:
        alert_channel = self._channel(self.config.alert_channel_id)
        if alert_channel is None:
            return
        try:
            await alert_channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not post alert: %s", exc)

    # A loop that keeps flapping (degrade -> recover -> degrade) only gets one
    # warning per ALERT_COOLDOWN, and a recovery note only follows a warning
    # that was actually posted. Stops #bot-config turning into a log file.
    ALERT_COOLDOWN = 6 * 3600

    async def _report(self, loop_health, ok: bool, error: str = "") -> None:
        """Feed a loop result to the tracker and emit alert/recovery messages."""
        name = loop_health.name
        if ok:
            recovered = loop_health.record_success()
            if recovered and self._warned.pop(name, False):
                embed = branding.dialog(
                    "System Recovered",
                    f"`{loop_health.name}` is responding normally again.",
                    color=branding.LIME,
                )
                await self._safe_alert(embed)
        else:
            newly_degraded = loop_health.record_failure(error)
            now = time.monotonic()
            if newly_degraded and now - self._last_warning.get(name, -1e9) >= self.ALERT_COOLDOWN:
                self._last_warning[name] = now
                self._warned[name] = True
                embed = branding.dialog(
                    "⚠ System Warning",
                    f"`{loop_health.name}` has failed "
                    f"{loop_health.consecutive_failures} times in a row.\n"
                    f"```{error[:300]}```",
                    color=branding.YELLOW,
                )
                await self._safe_alert(embed)

    # --- embed builders (reused by /testnotify) -----------------------------
    def live_embed(self, stream: Stream) -> discord.Embed:
        embed = branding.dialog(branding.LIVE_TITLE, color=branding.RED)
        embed.add_field(name="Title", value=stream.title or "Live now", inline=False)
        if stream.game_name:
            embed.add_field(name="Playing", value=stream.game_name, inline=True)
        embed.add_field(name="Watch", value=f"[twitch.tv/{stream.login}]({stream.url})", inline=True)
        if stream.thumbnail_url:
            embed.set_image(url=stream.thumbnail_url)
        return embed

    def ended_embed(self, base_title: str) -> discord.Embed:
        embed = branding.dialog(
            f"⚫ Stream Ended",
            f"{branding.OFFLINE_NOTE}\nThanks for stopping by. Catch the next one!",
            color=branding.SILVER,
        )
        return embed

    def clip_embed(self, clip: Clip) -> discord.Embed:
        embed = branding.dialog(branding.CLIP_TITLE, color=branding.NAVY)
        embed.add_field(name="Title", value=clip.title or "Untitled clip", inline=False)
        embed.add_field(name="Clipped by", value=clip.creator_name, inline=True)
        embed.add_field(name="Watch", value=f"[Open clip]({clip.url})", inline=True)
        if clip.thumbnail_url:
            embed.set_image(url=clip.thumbnail_url)
        return embed

    def video_embed(self, video: Video) -> discord.Embed:
        title = branding.SHORT_TITLE if video.is_short else branding.VIDEO_TITLE
        embed = branding.dialog(title, color=branding.RED if video.is_short else branding.NAVY)
        embed.add_field(name="Title", value=video.title or "New upload", inline=False)
        embed.add_field(name="Watch", value=f"[Open on YouTube]({video.url})", inline=True)
        if video.thumbnail_url:
            embed.set_image(url=video.thumbnail_url)
        return embed

    # --- LIVE loop ----------------------------------------------------------
    @tasks.loop(seconds=60)
    async def live_loop(self) -> None:
        try:
            stream = await self.twitch.get_live_stream(self.session)
        except Exception as exc:  # noqa: BLE001 - loops must never die
            await self._report(self.h_live, ok=False, error=repr(exc))
            return

        await self._report(self.h_live, ok=True)
        current_id = self.state.live_stream_id

        if stream is not None and stream.id != current_id:
            # A new stream just started.
            msg = await self._post(
                self.config.live_channel_id, self.config.live_role_id, self.live_embed(stream)
            )
            if msg is None:
                return  # not posted (permissions?); retry next tick
            self.state.set_live(msg.id if msg else None, stream.id)
        elif stream is None and current_id is not None:
            # The stream we announced has ended: edit the original post.
            channel = self._channel(self.config.live_channel_id)
            msg_id = self.state.live_message_id
            if channel is not None and msg_id is not None:
                try:
                    original = await channel.fetch_message(msg_id)
                    await original.edit(content=None, embed=self.ended_embed(""))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            self.state.set_live(None, None)

    # --- CLIPS loop ---------------------------------------------------------
    @tasks.loop(seconds=300)
    async def clips_loop(self) -> None:
        try:
            clips = await self.twitch.get_recent_clips(self.session)
        except Exception as exc:  # noqa: BLE001
            await self._report(self.h_clips, ok=False, error=repr(exc))
            return

        await self._report(self.h_clips, ok=True)

        if not self.state.is_primed("clips"):
            # First run after a fresh deploy: seed everything as seen, announce none.
            self.state.seed_seen("seen_clips", [c.id for c in clips])
            self.state.set_primed("clips")
            return

        # Announce oldest-first so the channel reads chronologically.
        for clip in reversed(clips):
            if self.state.has_seen("seen_clips", clip.id):
                continue
            msg = await self._post(
                self.config.clips_channel_id, self.config.clips_role_id, self.clip_embed(clip)
            )
            if msg is None:
                break  # retry next tick, keep order
            self.state.remember_clip(clip.id)

    # --- YOUTUBE loop -------------------------------------------------------
    @tasks.loop(seconds=300)
    async def youtube_loop(self) -> None:
        try:
            primed = self.state.is_primed("youtube")
            videos = await self.youtube.get_recent_videos(
                self.session,
                # Only probe /shorts/ for uploads we haven't posted yet.
                should_probe=lambda vid: primed and not self.state.has_seen("seen_videos", vid),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("YouTube poll failed: %s", exc)
            await self._report(self.h_youtube, ok=False, error=str(exc) or type(exc).__name__)
            return

        await self._report(self.h_youtube, ok=True)

        if not self.state.is_primed("youtube"):
            self.state.seed_seen("seen_videos", [v.id for v in videos])
            self.state.set_primed("youtube")
            return

        for video in reversed(videos):
            if self.state.has_seen("seen_videos", video.id):
                continue
            if video.is_short:
                msg = await self._post(
                    self.config.shorts_channel_id, self.config.shorts_role_id, self.video_embed(video)
                )
            else:
                msg = await self._post(
                    self.config.videos_channel_id, self.config.videos_role_id, self.video_embed(video)
                )
            if msg is None:
                break  # retry next tick, keep order
            self.state.remember_video(video.id)

    # --- gate loops until the bot is ready ----------------------------------
    @live_loop.before_loop
    @clips_loop.before_loop
    @youtube_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
