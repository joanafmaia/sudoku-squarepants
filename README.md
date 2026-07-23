# Sudoku Squarepants (Thcoku)

Interactive 9×9 Sudoku for Discord — slash-command bot **and** a Discord Activity web client.

## Architecture

| Piece | Host | Role |
|---|---|---|
| `bot.py` | **Fly.io** | Slash commands (`/play`, `/daily`, `/challenge`, …) |
| Activity (`activity/`) | **Netlify** | In-Discord web game (PyScript + Pygame-CE) |
| Backup | **MongoDB Atlas** | Leaderboard / challenges / dailies / sessions — shared by bot + Activity |

```
Discord slash  →  Fly.io (bot.py)  ─┐
                                    ├→  MongoDB Atlas
Discord Activity → Netlify (web+fn) ─┘
```

## Features

- **`/play`** — solo puzzle with difficulty tiers
- **`/daily`** — same difficulty each day, unique puzzle per player (anti-copy; weekday schedule)
- **`/challenge`** — private speedrun (invite players or open Join lobby, 2–5 players)
- Bikini Bottom UI — bubbly Fredoka digits, lagoon board, emoji border pins
- **XP** career score (leaderboard) + **sponges** spendable shop currency
- **`/shop`** — Titles (header flair) & Pins (border emojis only)
- **`/leaderboard`** — XP, today's daily, shop whales
- **Discord Activity** — play in-client; wins write XP/sponges to the same Mongo leaderboard
- HTTP `/health` for Fly.io health checks

## Setup (local bot)

1. Create a Discord application + bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Invite the bot with permissions: Send Messages, Embed Links, Attach Files, Read Message History, Create Private Threads, Send Messages in Threads, Manage Threads, Use Application Commands
3. Enable **Server Members Intent** (recommended for challenges)
4. Clone and install:

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with DISCORD_TOKEN + MONGODB_URI / MONGODB_DB
python bot.py
```

## Deploy bot on Fly.io

1. Install the [Fly CLI](https://fly.io/docs/hands-on/install-flyctl/) and run `fly auth login`
2. From the repo root (first time):

```bash
fly launch --no-deploy
# confirm app name matches fly.toml (sudoku-squarepants) or edit fly.toml
fly secrets set DISCORD_TOKEN=... MONGODB_URI=... MONGODB_DB=sudoku
# optional:
# fly secrets set DISCORD_GUILD_ID=your_server_id
fly deploy
```

3. Check health: `https://<app>.fly.dev/health` should return `ok`, and the bot should appear online in Discord.
4. Turn off the old Render service once Fly is healthy.

`fly.toml` keeps `min_machines_running = 1` and `auto_stop_machines = "off"` so the Discord gateway stays connected.

## Deploy Activity on Netlify

1. Import GitHub repo `joanafmaia/sudoku-squarepants` in [Netlify](https://app.netlify.com)
2. Build settings come from [`netlify.toml`](netlify.toml) (publish `activity/client/dist`)
3. **Environment variables** (Site settings):

| Variable | Scope | Description |
|---|---|---|
| `VITE_DISCORD_CLIENT_ID` | Build + Functions | OAuth2 application client ID |
| `DISCORD_CLIENT_SECRET` | Functions | OAuth2 client secret (never expose to the browser) |
| `MONGODB_URI` | Functions | Same Atlas URI as Fly |
| `MONGODB_DB` | Functions | Same DB name as Fly (e.g. `sudoku`) |

4. In Discord Developer Portal → your app → **Activities** → URL Mapping:
   - `/` → `https://YOUR-SITE.netlify.app`
   - `/api` → `https://YOUR-SITE.netlify.app`
5. Atlas **Network Access**: allow Fly + Netlify (often `0.0.0.0/0` for serverless).

Activity APIs:

- `POST /api/token` — OAuth code → access_token
- `GET /api/leaderboard` — top XP (optional `?guild_id=` / `?limit=`)
- `POST /api/activity/win` — award solo Activity win (`Authorization: Bearer <access_token>`)

### Local Activity preview

```bash
# terminal 1 — optional OAuth helper (or use Netlify Dev)
cd activity/server && npm install && npm run dev

# terminal 2
cd activity/client && npm install && npm run dev
```

Copy [`activity/.env.example`](activity/.env.example) → `activity/.env` with `VITE_DISCORD_CLIENT_ID` (and secret for the local server).

## Environment (bot / Fly)

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `DISCORD_GUILD_ID` | no | Server ID for instant slash sync (`0` = global only) |
| `MONGODB_URI` | recommended | Atlas URI (in-memory fallback if unset) |
| `MONGODB_DB` | no | Database name (default `sudoku`) |
| `PORT` | no | Health HTTP port (Fly sets / defaults to `8080`) |

Never commit `.env` — it is gitignored.

## Commands

`/help` · `/play` · `/daily` · `/challenge` · `/shop` · `/quit` · `/leaderboard` · `/stats` · `/testboard`
