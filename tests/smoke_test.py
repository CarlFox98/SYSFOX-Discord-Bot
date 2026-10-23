"""
SYSFOX smoke test (runs in CI on every push and pull request).

Boots every cog the same way main.py does, but never connects to Discord,
Twitch or YouTube. Catches the mistakes that would otherwise only show up
when the host restarts the bot: import errors, bad slash command
definitions, broken embed builders, and config parsing problems.

Run it locally with:  python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sysfox-smoke-")

# Dummy values for every required setting. Nothing here is a real secret.
os.environ.update(
    {
        "DISCORD_TOKEN": "smoke-test-token",
        "GUILD_ID": "1",
        "TWITCH_CLIENT_ID": "x",
        "TWITCH_CLIENT_SECRET": "x",
        "TWITCH_LOGIN": "NeoTheFox98",
        "YOUTUBE_CHANNEL_ID": "UCIJQfv1gmlQrdtmIyq170_g",
        "LIVE_CHANNEL_ID": "11",
        "CLIPS_CHANNEL_ID": "12",
        "VIDEOS_CHANNEL_ID": "13",
        "SHORTS_CHANNEL_ID": "14",
        "PANEL_CHANNEL_ID": "15",
        "ALERT_CHANNEL_ID": "16",
        "LIVE_ROLE_ID": "21",
        "CLIPS_ROLE_ID": "22",
        "VIDEOS_ROLE_ID": "23",
        "SHORTS_ROLE_ID": "24",
        "STATE_PATH": os.path.join(TMP, "state.json"),
        "GIVEAWAY_PATH": os.path.join(TMP, "giveaways.json"),
    }
)

import aiohttp  # noqa: E402
import discord  # noqa: E402

import feeds  # noqa: E402
from config import Config  # noqa: E402
from cogs import giveaways as gw  # noqa: E402
from cogs.admin import AdminCog  # noqa: E402
from cogs.notifications import NotificationsCog, _digest_time, _fmt_duration  # noqa: E402
from cogs.panel import PanelCog, RolePanel  # noqa: E402
from cogs.streamtools import StreamToolsCog  # noqa: E402
from main import SysfoxBot  # noqa: E402

EXPECTED_COMMANDS = {
    "status", "testnotify", "postpanel", "reboot", "ping", "config", "state",
    "guilds", "delay", "cancelstream", "digest", "giveaway",
}

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def embed_ok(embed: discord.Embed, what: str) -> None:
    check(len(embed) <= 6000 and len(embed.fields) <= 25, f"{what} fits Discord's limits")
    for f in embed.fields:
        if not (f.name and f.value) or len(f.name) > 256 or len(f.value) > 1024:
            check(False, f"{what}: field {f.name!r} is empty or too long")


async def run() -> None:
    config = Config.load()
    check(config.digest_hour == 9 and config.fail_threshold_youtube == 12, "config defaults load")
    check(_digest_time(config).tzinfo is not None, "digest time has a timezone")

    bot = SysfoxBot(config)
    async with aiohttp.ClientSession() as session:
        bot.session = session
        bot.add_view(RolePanel(config))
        await bot.add_cog(PanelCog(bot, config))
        await bot.add_cog(AdminCog(bot, config, bot.tracker))
        notif = NotificationsCog(bot, config, bot.state, bot.tracker, session)
        await bot.add_cog(notif)
        await bot.add_cog(StreamToolsCog(bot, config))
        await gw.setup_giveaways(bot)

        # --- slash commands -------------------------------------------------
        names = {c.name for c in bot.tree.get_commands()}
        check(EXPECTED_COMMANDS <= names, f"all slash commands registered (missing: {EXPECTED_COMMANDS - names or 'none'})")
        for cmd in bot.tree.get_commands():
            try:
                cmd.to_dict(bot.tree)
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                print("        ", repr(exc))
            check(ok, f"/{cmd.name} serializes for Discord")
        for cog in (bot.get_cog("AdminCog"), bot.get_cog("StreamToolsCog"), notif, bot.get_cog("GiveawaysCog")):
            check(
                getattr(cog, "__discord_app_commands_error_handler__", None) is not None,
                f"{type(cog).__name__} has an app command error handler",
            )
        hidden = [c.name for c in bot.tree.get_commands() if c.default_permissions is None]
        check(not hidden, f"staff commands are hidden from members (visible: {hidden or 'none'})")

        # --- embeds ---------------------------------------------------------
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 3600))
        stream = feeds.Stream(
            id="1", title="Smoke test stream", game_name="Satisfactory", started_at=started,
            thumbnail_url="https://example.com/t.jpg", login="NeoTheFox98", viewer_count=12,
        )
        embed_ok(notif.live_embed(stream, peak=20), "go-live embed")
        embed_ok(notif._live_embed_for(stream, {"started_at": started, "peak": 20}), "refreshed go-live embed")
        meta = {"title": "Smoke test stream", "game": "Satisfactory", "started_at": started, "peak": 20}
        ended = notif.ended_embed(meta=meta, vod_url="https://twitch.tv/videos/1")
        embed_ok(ended, "stream-ended embed")
        check(any(f.name == "Streamed for" and f.value.startswith("3h") for f in ended.fields), "stream duration is right")
        embed_ok(notif.ended_embed(""), "stream-ended embed with no details")
        embed_ok(notif.clip_embed(feeds.Clip("c", "Clip", "https://x", "", "someone", started)), "clip embed")
        embed_ok(notif.video_embed(feeds.Video("v", "Video", "https://x", started, "", True)), "short embed")

        # --- health tracking and the daily report ---------------------------
        for _ in range(3):
            await notif._report(notif.h_youtube, ok=False, error="YouTube feed returned HTTP 404")
        await notif._report(notif.h_youtube, ok=True)
        await notif._report(notif.h_live, ok=True)
        digest = notif.build_digest()
        embed_ok(digest, "daily report embed")
        yt = next(f for f in digest.fields if f.name == "YouTube")
        check("1/4" in yt.value and "15m" in yt.value, "daily report counts checks and the longest outage")
        check(_fmt_duration(3 * 3600 + 5 * 60) == "3h 5m", "duration formatting")

        # --- state ------------------------------------------------------------
        bot.state.set_live(123, "1")
        bot.state.set_live_meta(meta)
        stats = bot.state.stats()
        check(stats["live_stream_id"] == "1" and stats["file_bytes"] > 0, "/state reads real numbers")
        check(bot.state.live_meta.get("peak") == 20, "live details survive in state")

        # --- reconnect handling ----------------------------------------------
        check(notif._reconnect_target("1") == (123, bot.state.live_meta), "a stream that is still tracked gets reused")
        bot.state.set_live(None, None)
        notif._last_ended = (123, time.monotonic(), dict(meta))
        check(notif._reconnect_target(None) is not None, "a quick reconnect reuses the old post")
        notif._last_ended = (123, time.monotonic() - 3600, dict(meta))
        check(notif._reconnect_target(None) is None, "an old stream is not reused")

        # --- giveaway logic ---------------------------------------------------
        g = {"entrants": [1, 2, 3], "host_id": 3, "past_winners": [1]}
        check(gw.eligible_entrants(g) == [2], "giveaway excludes the host and past winners")
        check(gw.draw({"entrants": [], "host_id": 0}) is None, "giveaway with no entries has no winner")

        for cog_name in ("NotificationsCog", "GiveawaysCog"):
            await bot.remove_cog(cog_name)  # stops the background loops

    await bot.health_server.stop()


asyncio.run(run())
if failures:
    print(f"\n{len(failures)} check(s) failed.")
    sys.exit(1)
print("\nAll smoke checks passed.")
