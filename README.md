# Sudoku Discord Bot

Interactive 9×9 Sudoku for Discord — solo, daily, and multiplayer speedrun challenges.

## Features

- **`/play`** — solo puzzle with difficulty tiers
- **`/daily`** — shared daily puzzle (weekday difficulty schedule)
- **`/challenge`** — private speedrun (invite players or open Join lobby, 2–5 players)
- Paper & Pencil UI (board image + button stages)
- Coins, shop (titles & hints), leaderboards
- Optional MongoDB for challenges / daily claims / session restore

## Setup

1. Create a Discord application + bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Invite the bot with permissions: Send Messages, Embed Links, Attach Files, Read Message History, Create Private Threads, Send Messages in Threads, Manage Threads, Use Application Commands
3. Enable **Server Members Intent** (recommended for challenges)
4. Clone and install:

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your DISCORD_TOKEN (and optional Mongo / announce channel)
python bot.py
```

## Environment

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `MONGODB_URI` | no | Atlas/local URI (in-memory fallback if unset) |
| `MONGODB_DB` | no | Database name (default `sudoku`) |
| `DAILY_ANNOUNCE_CHANNEL_ID` | no | Public channel for daily clear announcements (`0` = off) |

Never commit `.env` — it is gitignored.

## Commands

`/help` · `/play` · `/daily` · `/challenge` · `/hint` · `/shop` · `/quit` · `/leaderboard` · `/stats` · `/dailyboard`
