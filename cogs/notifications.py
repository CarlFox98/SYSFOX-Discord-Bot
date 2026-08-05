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

import discord
from discord.ext import commands, tasks

import branding
from config import Config
from feeds import Clip, Stream, TwitchClient, Video, YouTubeClient
from health import HealthTracker
from state import State


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
        channel = self._channel(channel_id)
        if channel is None:
            return None
        return await channel.send(
            content=f"<@&{role_id}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
        )

    async def _report(self, loop_health, ok: bool, error: str = "") -> None:
        """Feed a loop result to the tracker and emit alert/recovery messages."""
        alert_channel = self._channel(self.config.alert_channel_id)
        if ok:
            recovered = loop_health.record_success()
            if recovered and alert_channel is not None:
                embed = branding.dialog(
                    "System Recovered",
                    f"`{loop_health.name}` is responding normally again.",
                    color=branding.LIME,
                )
                await alert_channel.send(embed=embed)
        else:
            newly_degraded = loop_health.record_failure(error)
            if newly_degraded and alert_channel is not None:
                embed = branding.dialog(
                    "⚠ System Warning",
                    f"`{loop_health.name}` has failed "
                    f"{loop_health.consecutive_failures} times in a row.\n"
                    f"```{error[:300]}```",
                    color=branding.YELLOW,
                )
                await alert_channel.send(embed=embed)

    # --- embed builders (reused by /testnotify) -----------------------------
    def live_embed(self, stream: Stream) -> discord.Embed:
        embed = branding.dialog(branding.LIVE_TITLE, color=branding.RED)
        embed.add_field(name="Title", value=stream.title or "—", inline=False)
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
        embed.add_field(name="Title", value=clip.title or "—", inline=False)
        embed.add_field(name="Clipped by", value=clip.creator_name, inline=True)
        embed.add_field(name="Watch", value=f"[Open clip]({clip.url})", inline=True)
        if clip.thumbnail_url:
            embed.set_image(url=clip.thumbnail_url)
        return embed

    def video_embed(self, video: Video) -> discord.Embed:
        title = branding.SHORT_TITLE if video.is_short else branding.VIDEO_TITLE
        embed = branding.dialog(title, color=branding.RED if video.is_short else branding.NAVY)
        embed.add_field(name="Title", value=video.title or "—", inline=False)
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
            await self._post(
                self.config.clips_channel_id, self.config.clips_role_id, self.clip_embed(clip)
            )
            self.state.remember_clip(clip.id)

    # --- YOUTUBE loop -------------------------------------------------------
    @tasks.loop(seconds=300)
    async def youtube_loop(self) -> None:
        try:
            videos = await self.youtube.get_recent_videos(self.session)
        except Exception as exc:  # noqa: BLE001
            await self._report(self.h_youtube, ok=False, error=repr(exc))
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
                await self._post(
                    self.config.shorts_channel_id, self.config.shorts_role_id, self.video_embed(video)
                )
            else:
                await self._post(
                    self.config.videos_channel_id, self.config.videos_role_id, self.video_embed(video)
                )
            self.state.remember_video(video.id)

    # --- gate loops until the bot is ready ----------------------------------
    @live_loop.before_loop
    @clips_loop.before_loop
    @youtube_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
