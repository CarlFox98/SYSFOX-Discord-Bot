"""
SYSFOX :: notification loops

Background loops:
  * live    - Twitch go-live; keeps the post current (title, game, viewers) and
              turns it into a recap with duration and a VOD link when it ends
  * digest  - one quiet daily report in the alert channel (DIGEST_HOUR, local time)
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
from datetime import datetime, time as dtime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:  # tzdata is in requirements; fall back to UTC if zone data is missing
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

import branding
from config import Config
from feeds import Clip, Stream, TwitchClient, Video, YouTubeClient
from health import HealthTracker
from state import State

log = logging.getLogger("sysfox")

# How often the go-live post refreshes its viewer count and thumbnail.
LIVE_EDIT_INTERVAL = 300
# Offline polls in a row before a stream counts as ended. Twitch sometimes drops
# a live stream from the API for a single poll; without this, a blip would mark
# the stream ended and the next poll would ping everyone a second time.
OFFLINE_POLLS_TO_END = 3
# If a new stream starts this soon after the last one ended (OBS crash, internet
# drop), it's treated as the same stream: the old post comes back to life and
# nobody gets pinged twice.
RECONNECT_GRACE = 15 * 60


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _digest_time(config: Config) -> dtime:
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(config.digest_tz)
        except Exception:  # noqa: BLE001 - bad tz name: fall back to UTC
            log.warning("Unknown DIGEST_TZ %r, using UTC", config.digest_tz)
    return dtime(hour=config.digest_hour, minute=0, tzinfo=tz)


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

        # Live-post bookkeeping (in memory; started_at/peak live in state).
        self._last_live_edit = 0.0
        self._offline_streak = 0
        # Last ended stream, so a quick reconnect can reuse its post:
        # (message_id, ended_monotonic, meta)
        self._last_ended: tuple[int, float, dict] | None = None

        # Daily digest counters, reset after each digest.
        self._reset_stats()
        self._intervals = {
            "twitch_live": config.poll_interval_live,
            "twitch_clips": config.poll_interval_clips,
            "youtube": config.poll_interval_youtube,
        }
        self._ping_labels = {
            config.live_channel_id: "Go-live",
            config.clips_channel_id: "Clips",
            config.videos_channel_id: "Videos",
            config.shorts_channel_id: "Shorts",
        }

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
        self.digest_loop.change_interval(time=_digest_time(config))

    def cog_unload(self) -> None:
        self.live_loop.cancel()
        self.clips_loop.cancel()
        self.youtube_loop.cancel()
        self.digest_loop.cancel()

    async def cog_load(self) -> None:
        self.live_loop.start()
        self.clips_loop.start()
        self.youtube_loop.start()
        if self.config.digest_enabled:
            self.digest_loop.start()

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
            msg = await channel.send(
                content=f"<@&{role_id}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
            label = self._ping_labels.get(channel_id, "Other")
            self._pings[label] = self._pings.get(label, 0) + 1
            return msg
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
            self._alerts_sent += 1
        except discord.HTTPException as exc:
            log.warning("Could not post alert: %s", exc)

    # A loop that keeps flapping (degrade -> recover -> degrade) only gets one
    # warning per ALERT_COOLDOWN, and a recovery note only follows a warning
    # that was actually posted. Stops #bot-config turning into a log file.
    ALERT_COOLDOWN = 6 * 3600

    async def _report(self, loop_health, ok: bool, error: str = "") -> None:
        """Feed a loop result to the tracker and emit alert/recovery messages."""
        name = loop_health.name
        st = self._stats.setdefault(name, {"ok": 0, "fail": 0, "streak": 0, "max_streak": 0})
        if ok:
            st["ok"] += 1
            st["streak"] = 0
        else:
            st["fail"] += 1
            st["streak"] += 1
            st["max_streak"] = max(st["max_streak"], st["streak"])

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
    def live_embed(self, stream: Stream, *, peak: int | None = None) -> discord.Embed:
        embed = branding.dialog(branding.LIVE_TITLE, color=branding.RED)
        embed.add_field(name="Title", value=stream.title or "Live now", inline=False)
        if stream.game_name:
            embed.add_field(name="Playing", value=stream.game_name, inline=True)
        if stream.viewer_count:
            viewers = f"{stream.viewer_count}"
            if peak and peak > stream.viewer_count:
                viewers += f" (peak {peak})"
            embed.add_field(name="Watching", value=viewers, inline=True)
        started = _parse_ts(stream.started_at)
        if started is not None:
            embed.add_field(name="Live since", value=f"<t:{int(started.timestamp())}:R>", inline=True)
        embed.add_field(name="Watch", value=f"[twitch.tv/{stream.login}]({stream.url})", inline=True)
        if stream.thumbnail_url:
            # Twitch keeps the same thumbnail URL all stream; a changing query
            # string makes Discord fetch a fresh preview on each refresh.
            embed.set_image(url=f"{stream.thumbnail_url}?t={int(time.time() // LIVE_EDIT_INTERVAL)}")
        return embed

    def ended_embed(
        self,
        base_title: str = "",
        *,
        meta: dict | None = None,
        vod_url: str | None = None,
        ended_at: datetime | None = None,
    ) -> discord.Embed:
        meta = meta or {}
        lines = [branding.OFFLINE_NOTE]
        title = meta.get("title") or base_title
        if title:
            lines.append(f"**{title}**")
        if meta.get("game"):
            lines.append(f"Playing {meta['game']}")
        embed = branding.dialog("⚫ Stream Ended", "\n".join(lines), color=branding.SILVER)
        started = _parse_ts(meta.get("started_at", ""))
        if started is not None:
            duration = (ended_at or datetime.now(timezone.utc)) - started
            embed.add_field(name="Streamed for", value=_fmt_duration(duration.total_seconds()), inline=True)
        if meta.get("peak"):
            embed.add_field(name="Peak viewers", value=str(meta["peak"]), inline=True)
        login = meta.get("login") or self.config.twitch_login
        vod = vod_url or f"https://twitch.tv/{login}/videos"
        embed.add_field(
            name="Missed it?",
            value=f"[Watch the VOD]({vod})" if vod_url else f"[Past broadcasts]({vod})",
            inline=True,
        )
        embed.add_field(
            name="\u200b", value="Thanks for stopping by. Catch the next one!", inline=False
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
    def _reconnect_target(self, current_id: str | None) -> tuple[int, dict] | None:
        """Return (message_id, meta) to reuse if this 'new' stream is really a
        reconnect: either the previous stream hasn't been marked ended yet, or
        it ended within RECONNECT_GRACE."""
        if current_id is not None and self.state.live_message_id is not None:
            return self.state.live_message_id, self.state.live_meta
        if self._last_ended is not None:
            msg_id, ended_at, meta = self._last_ended
            if time.monotonic() - ended_at <= RECONNECT_GRACE:
                return msg_id, dict(meta)
        return None

    def _live_embed_for(self, stream: Stream, meta: dict) -> discord.Embed:
        """Live embed that keeps the original start time across reconnects."""
        started = meta.get("started_at") or stream.started_at
        view = Stream(
            id=stream.id, title=stream.title, game_name=stream.game_name,
            started_at=started, thumbnail_url=stream.thumbnail_url,
            login=stream.login, viewer_count=stream.viewer_count,
        )
        return self.live_embed(view, peak=int(meta.get("peak") or 0))

    async def _edit_live_post(
        self, embed: discord.Embed, *, clear_ping: bool = False, restore_ping: bool = False
    ) -> None:
        channel = self._channel(self.config.live_channel_id)
        msg_id = self.state.live_message_id
        if channel is None or msg_id is None or not hasattr(channel, "get_partial_message"):
            return
        try:
            kwargs = {"embed": embed}
            if clear_ping:
                kwargs["content"] = None
            elif restore_ping:
                # Editing a mention back in does not ping anyone again.
                kwargs["content"] = f"<@&{self.config.live_role_id}>"
                kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            await channel.get_partial_message(msg_id).edit(**kwargs)
        except (discord.NotFound, discord.Forbidden):
            pass  # post was deleted or we lost access; nothing to update
        except discord.HTTPException as exc:
            log.warning("Could not edit go-live post: %s", exc)

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
            reconnect = self._reconnect_target(current_id)
            if reconnect is not None:
                # Same session, new Twitch stream id (dropped and came back).
                msg_id, meta = reconnect
                log.info("Stream reconnected (%s); reusing go-live post %s", stream.id, msg_id)
                self.state.set_live(msg_id, stream.id)
                meta.update(title=stream.title, game=stream.game_name, login=stream.login,
                            peak=max(int(meta.get("peak") or 0), stream.viewer_count))
                meta.setdefault("started_at", stream.started_at)
                self.state.set_live_meta(meta)
                self._last_ended = None
                self._offline_streak = 0
                self._last_live_edit = time.monotonic()
                await self._edit_live_post(self._live_embed_for(stream, meta), restore_ping=True)
                return

            # A new stream just started.
            msg = await self._post(
                self.config.live_channel_id, self.config.live_role_id, self.live_embed(stream)
            )
            if msg is None:
                return  # not posted (permissions?); retry next tick
            self.state.set_live(msg.id, stream.id)
            self.state.set_live_meta(
                {
                    "started_at": stream.started_at,
                    "title": stream.title,
                    "game": stream.game_name,
                    "peak": stream.viewer_count,
                    "login": stream.login,
                }
            )
            self._last_live_edit = time.monotonic()
            self._offline_streak = 0

        elif stream is not None:
            # Still live: keep the post current.
            self._offline_streak = 0
            meta = self.state.live_meta
            changed = stream.title != meta.get("title") or stream.game_name != meta.get("game")
            peak = max(int(meta.get("peak") or 0), stream.viewer_count)
            if changed or peak != meta.get("peak"):
                meta.update(
                    title=stream.title, game=stream.game_name, peak=peak,
                    started_at=meta.get("started_at") or stream.started_at, login=stream.login,
                )
                self.state.set_live_meta(meta)
            now = time.monotonic()
            if changed or now - self._last_live_edit >= LIVE_EDIT_INTERVAL:
                self._last_live_edit = now
                await self._edit_live_post(self._live_embed_for(stream, meta))

        elif current_id is not None:
            # Looks offline. Wait a few polls so an API blip can't end the stream.
            self._offline_streak += 1
            if self._offline_streak < OFFLINE_POLLS_TO_END:
                return
            meta = self.state.live_meta
            vod_url = None
            try:
                vod_url = await self.twitch.get_vod_url(self.session, current_id)
            except Exception as exc:  # noqa: BLE001 - VOD link is a nice-to-have
                log.info("No VOD link for stream %s: %s", current_id, exc)
            # It actually went offline at the first of the offline polls.
            ended_at = datetime.fromtimestamp(
                time.time() - (OFFLINE_POLLS_TO_END - 1) * self.config.poll_interval_live, timezone.utc
            )
            await self._edit_live_post(
                self.ended_embed(meta=meta, vod_url=vod_url, ended_at=ended_at), clear_ping=True
            )
            msg_id = self.state.live_message_id
            if msg_id is not None:
                self._last_ended = (msg_id, time.monotonic(), dict(meta))
            self.state.set_live(None, None)
            self.state.set_live_meta({})
            self._offline_streak = 0

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

    # --- DAILY DIGEST -------------------------------------------------------
    def _reset_stats(self) -> None:
        self._stats: dict[str, dict[str, int]] = {}
        self._pings: dict[str, int] = {}
        self._alerts_sent = 0
        self._stats_since = time.time()

    def build_digest(self) -> discord.Embed:
        hours = (time.time() - self._stats_since) / 3600
        window = "Last 24 hours" if hours >= 23 else f"Since restart ({_fmt_duration(hours * 3600)} ago)"
        worst = branding.LIME
        embed = branding.dialog("📋 Daily Report", window, color=branding.LIME)

        labels = {"twitch_live": "Twitch live", "twitch_clips": "Twitch clips", "youtube": "YouTube"}
        for name, label in labels.items():
            st = self._stats.get(name, {"ok": 0, "fail": 0, "streak": 0, "max_streak": 0})
            total = st["ok"] + st["fail"]
            lh = self.tracker.loops.get(name)
            if lh is not None and lh.degraded:
                icon, worst = "🔴", branding.RED
                extra = "\ndown right now"
            elif st["fail"]:
                icon = "🟡"
                if worst == branding.LIME:
                    worst = branding.YELLOW
                longest = _fmt_duration(st["max_streak"] * self._intervals.get(name, 60))
                extra = f"\nlongest outage {longest}"
            else:
                icon, extra = "🟢", ""
            value = f"{icon} {st['ok']}/{total} checks OK{extra}" if total else "⚪ no checks yet"
            embed.add_field(name=label, value=value, inline=True)

        pings = " · ".join(
            f"{k} {self._pings.get(k, 0)}" for k in ("Go-live", "Clips", "Videos", "Shorts")
        )
        embed.add_field(name="Pings sent", value=pings, inline=False)
        if self._alerts_sent:
            embed.add_field(name="Alerts posted", value=str(self._alerts_sent), inline=True)
        embed.color = worst
        return embed

    @tasks.loop(time=dtime(hour=9))
    async def digest_loop(self) -> None:
        channel = self._channel(self.config.alert_channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=self.build_digest())
        except discord.HTTPException as exc:
            log.warning("Could not post daily digest: %s", exc)
            return
        self._reset_stats()

    @app_commands.command(name="digest", description="Preview the daily report now (just for you).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def digest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.build_digest(), ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to use that."
        else:
            log.warning("notifications command failed: %r", error)
            msg = f"⚠️ {type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @digest_loop.before_loop
    async def _before_digest(self) -> None:
        await self.bot.wait_until_ready()

    # --- gate loops until the bot is ready ----------------------------------
    @live_loop.before_loop
    @clips_loop.before_loop
    @youtube_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
