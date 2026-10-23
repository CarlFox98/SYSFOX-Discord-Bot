# SYSFOX: Neo's Den Discord Bot

The notification bot for **Neo's Den**. It watches Twitch and YouTube, posts
opt-in pings into the server, and runs the den's giveaways, all with a
Windows 98 look.

## What it does

**Stream and upload pings**
- 🔴 **Twitch go-live.** The post stays current while you're live (viewers and
  peak, live-since time, title and game changes, a fresh thumbnail every 5 min)
  and turns into a recap when the stream ends: duration, peak viewers, and a
  VOD link.
- 🔁 **Reconnect-safe.** If the stream drops and comes back within 15 minutes,
  the same post is reused and nobody gets pinged twice. A one-poll Twitch API
  blip can't end a stream either.
- ✂️ **Twitch clips**, 📼 **YouTube videos**, and ⚡ **YouTube Shorts**, each
  in its own channel with its own ping role.
- 🎛️ **Opt-in panel.** Members toggle Go-Live, Clips, Videos, Shorts, and
  Giveaway pings with buttons that keep working across restarts.

**Giveaways**
- 🎁 `/giveaway start` posts a giveaway with a 🎉 Enter button. SYSFOX enforces
  the entry rules, draws a random winner, hands them the code privately with a
  Claim button, and rerolls on its own if nobody claims in 48 hours.
- 🗓️ `/giveaway queue` lines up prizes that auto-start on the 1st of each month.
- 📝 Every step is logged to the mod log. Codes are never logged or posted.

**Keeping an eye on itself**
- 📋 **Daily report** in the alert channel each morning: checks OK per feed,
  longest outage, pings sent, and alerts posted.
- ⚠️ **Outage alerts** only for real outages, at most one warning per feed
  every 6 hours, with a recovery note when it's back.
- ❤️ **Health endpoint** at `/health` for UptimeRobot.

## Slash commands

All of them need the **Manage Server** permission, and replies are private
unless noted.

| Command | What it does |
|---|---|
| `/delay minutes [note]` | Posts "running late, new start at…" in the go-live channel and pings @Live (public) |
| `/cancelstream [reason]` | Posts "no stream today" in the go-live channel and pings @Live (public) |
| `/giveaway start` · `queue` · `list` · `end` · `reroll` · `cancel` · `unqueue` | Run and manage giveaways |
| `/status` | Health of each feed, uptime, failure counts |
| `/digest` | Preview the daily report right now |
| `/testnotify` | Preview a Go-live, Stream ended, Clip, Video, or Short card (no ping) |
| `/postpanel` | Re-post the opt-in panel |
| `/ping` · `/config` · `/state` · `/guilds` | Latency, loaded settings (tokens hidden), state stats, servers |
| `/reboot` | Restart the bot process |

## Project layout

```
main.py                 entry point (boots everything in setup_hook)
config.py               loads and validates env vars
branding.py             Win98 palette and embed helpers
state.py                "already announced" tracking and the live post
health.py               health tracking and the /health server
feeds.py                Twitch (Helix) and YouTube (RSS) fetching
cogs/notifications.py   polling loops, go-live post, alerts, daily report
cogs/streamtools.py     /delay and /cancelstream
cogs/giveaways.py       /giveaway and the claim flow
cogs/panel.py           opt-in role button panel
cogs/admin.py           /status, /testnotify, /postpanel and friends
requirements.txt        pinned dependencies
.env.example            template for every setting
```

## Configuration

Every setting is an environment variable. [`.env.example`](.env.example) lists
them all. The required ones are the Discord token and server ID, the Twitch app
credentials, the YouTube channel ID, six channel IDs, and four role IDs.
Everything else has a default that fits Neo's Den.

Where to get the values:
- **Discord token and IDs:** the [Developer Portal](https://discord.com/developers/applications).
  Turn on Developer Mode in Discord, then right-click a channel or role and
  pick *Copy ID*.
- **Twitch app:** [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)
  gives you a Client ID and Client Secret. It uses app credentials, so no
  Twitch login is needed.
- **YouTube:** just the channel ID (`UCIJQfv1gmlQrdtmIyq170_g`). SYSFOX reads
  the public RSS feed, so there's no API key.

SYSFOX needs **Manage Roles**, and its role has to sit **above** the ping roles
in Server Settings → Roles, or it can't hand them out. It also needs to be able
to send messages and embeds in every channel it posts to.

## Deploying on bot-hosting.net

GitHub is the source of truth. The host pulls `main` every time the bot
starts, so updating is just push, then restart.

**First-time setup**
1. In the panel's **Startup** tab, set the git repo address to
   `https://github.com/CarlFox98/SYSFOX-Discord-Bot` and the branch to `main`.
   Turn on auto-pull. A git username and token are only needed if the repo
   is private.
2. Add every variable from `.env.example` under **Environment Variables** in
   the same tab. They survive reinstalls and never touch git.
3. Set the startup file to `main.py`.
4. Run **Settings → Reinstall Server** once. The host clones the repo and
   installs `requirements.txt`.

**Shipping a change**
1. Push to `main`.
2. Hit **Restart** in the panel. The new code is pulled on startup.

> ⚠️ **Don't use Reinstall for updates.** Reinstall wipes `data/`, which holds
> the giveaway codes and queue plus the "already announced" lists. Restart is
> all you need. If you ever do lose `data/`, the host's **Backups** tab keeps
> automatic restore points about every hour.

## External monitoring (UptimeRobot)

Point an UptimeRobot HTTP monitor at:

```
http://<your-host>:<HEALTH_PORT>/health
```

It returns JSON like this (raw error text is left out on purpose, `/status`
in Discord has the details):

```json
{ "status": "ok", "uptime_seconds": 1234, "guilds": 1,
  "loops": { "twitch_live": {"degraded": false, "...": "..."}, "youtube": {"...": "..."} } }
```

The endpoint answers `200` whenever the process is alive, so UptimeRobot tells
you when the bot is down entirely. Trouble with a single feed shows up in the
alert channel, the daily report, and `/status`.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in .env
python main.py
```

## Notes

- **YouTube is flaky from datacenter IPs.** Its RSS feed returns 404s and 5xxs
  for stretches, often in the evening (US Eastern). SYSFOX makes one request per
  check, only flags YouTube after about an hour of straight failures
  (`FAIL_THRESHOLD_YOUTUBE`), and logs the rest quietly.
- **First run priming.** After a fresh deploy, clips and YouTube mark whatever
  is already published as seen, so there's no burst of old pings. The next new
  upload or clip posts normally.
- **Shorts detection.** A new upload counts as a Short only if its `/shorts/`
  URL loads without redirecting. If it's unclear, it's posted as a normal video
  so nothing gets dropped.
