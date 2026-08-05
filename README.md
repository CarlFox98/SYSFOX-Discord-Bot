# SYSFOX — Neo's Den Discord Bot

The notification bot for **Neo's Den**. Watches Twitch and YouTube and posts
opt-in-pinged announcements into the server, with a Windows 98 aesthetic.

- 🔴 Twitch **go-live** (and edits the post to "stream ended" when the stream stops)
- ✂️ Twitch **clips**
- 📼 YouTube **videos**
- ⚡ YouTube **Shorts**
- 🎛️ Persistent **role opt-in panel** (buttons survive restarts)
- ❤️ **Health endpoint** at `/health` for external monitoring (UptimeRobot)
- 🧰 Slash commands: `/status`, `/testnotify`, `/postpanel`

## Why this repo is the source of truth

The bot is designed so the **code lives in GitHub** and the host pulls from it.
If a deployment is ever wiped, you reinstall and it re-clones — no rebuilding
from scratch. Secrets never live in the repo; they're set as environment
variables on the host.

## Project layout

```
main.py                 entry point (boots everything in setup_hook)
config.py               loads/validates env vars
branding.py             Win98 palette + embed helpers
state.py                persistent "seen" tracking + live-message tracking
health.py               HealthTracker + aiohttp /health server
feeds.py                Twitch (Helix) + YouTube (RSS) fetching
cogs/notifications.py   the four polling loops + degradation alerts
cogs/panel.py           persistent role opt-in button panel
cogs/admin.py           /status, /testnotify, /postpanel
requirements.txt        pinned dependencies
.env.example            documented template for every variable
```

## Configuration

Every setting is an environment variable — see [`.env.example`](.env.example)
for the full list. Required: the Discord token + guild id, Twitch app
credentials, the YouTube channel id, and the six channel ids and four role ids.

Get the values:
- **Discord token & IDs** — [Developer Portal](https://discord.com/developers/applications).
  Enable Developer Mode in Discord to right-click → *Copy ID* for channels/roles.
- **Twitch app** — [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)
  gives you the Client ID and Client Secret (client-credentials flow; no user login).
- **YouTube** — just the channel id (`UCIJQfv1gmlQrdtmIyq170_g`); the bot reads the
  public RSS feed, so no API key.

The bot needs the **Manage Roles** permission, and its own role must sit **above**
the four opt-in roles in Server Settings → Roles, or it can't assign them.

## Deploy on bot-hosting.net (GitHub pull)

This is the setup that makes wipes harmless.

1. Push this repo to `https://github.com/CarlFox98/SYSFOX-Discord-Bot`.
2. In the bot-hosting.net panel, open the **Startup** tab and set:
   - **Git repo address:** `https://github.com/CarlFox98/SYSFOX-Discord-Bot`
   - **Install branch:** `main`
   - **Git username / access token:** only needed if the repo is private
     (create a token at <https://github.com/settings/tokens>).
3. Add every variable from `.env.example` under **Environment Variables** in the
   same Startup tab. (These persist across reinstalls and stay out of git.)
4. Empty the file manager, then go to **Settings → Reinstall Server**. The host
   clones the repo and installs `requirements.txt`.
5. Set the startup command to run `main.py` (e.g. `python main.py`).

Whenever you push a change to `main`, redeploy/reinstall to pull it. If a
deployment is ever lost, step 4 brings everything back.

> **Recovering the old bot:** if a previous `main.py` is still sitting in a
> running deployment's file manager, download it before reinstalling — reinstall
> empties the file manager first.

## External monitoring (UptimeRobot)

Point an UptimeRobot HTTP(s) monitor at the health endpoint:

```
http://<your-host>:<HEALTH_PORT>/health
```

It returns JSON like:

```json
{ "status": "ok", "uptime_seconds": 1234, "guilds": 1,
  "loops": { "twitch_live": {"degraded": false, ...}, "youtube": {...} } }
```

The endpoint always answers `200` when the process is alive, so UptimeRobot
alerts you if the bot goes *down entirely* — the safety net for a wiped or
crashed deployment. Per-loop trouble (e.g. Twitch API hiccups) is surfaced
separately as a message in your alert channel and via `/status`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in .env
python main.py
```

## Notes on tuning

- **YouTube 404s:** the RSS feed and the Shorts URL probe both 404 intermittently
  from datacenter IPs. The YouTube loop tolerates **6** consecutive failures
  (~30 min) before flagging degraded, versus **3** for Twitch/clips, to avoid
  false alarms. Adjust in `config.py` if needed.
- **First run priming:** after a fresh deploy the clip/YouTube loops mark whatever
  is currently published as "already seen" so you don't get a burst of stale
  pings. The very next new upload/clip announces normally.
- **Shorts detection:** a video is treated as a Short only if its `/shorts/` URL
  resolves without redirecting; when uncertain it's posted as a normal video so a
  notification is never dropped.
