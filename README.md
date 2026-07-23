# Sudoku Squarepants (Thcoku)

Interactive 9×9 Sudoku for Discord — slash-command bot **and** a Discord Activity web client.

## Architecture

| Piece | Host | Role |
|---|---|---|
| `bot.py` | **Render** (Free + UptimeRobot) | Slash commands (`/play`, `/daily`, `/challenge`, …) |
| Activity (`activity/`) | **Netlify** | In-Discord web game (PyScript + Pygame-CE) |
| Backup | **MongoDB Atlas** | Leaderboard / challenges / dailies / sessions — shared by bot + Activity |

```
Discord slash  →  Render (bot.py)   ─┐
                                     ├→  MongoDB Atlas
Discord Activity → Netlify (web+fn) ─┘
```

## Features

- **`/play`** — opens the Discord Activity window (like Wordle); classic board → `/classic`
- **`/daily`** — same difficulty each day, unique puzzle per player (anti-copy; weekday schedule)
- **`/challenge`** — private speedrun (invite players or open Join lobby, 2–5 players)
- Bikini Bottom UI — bubbly Fredoka digits, lagoon board, emoji border pins
- **XP** career score (leaderboard) + **sponges** spendable shop currency
- **`/shop`** — Titles (header flair) & Pins (border emojis only)
- **`/leaderboard`** — XP, today's daily, shop whales
- **Discord Activity** — play in-client; wins write XP/sponges to the same Mongo leaderboard
- HTTP `/health` — Render health checks + UptimeRobot keep-alive

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

## Deploy bot on Render (Free)

1. Push this repo to GitHub, then in [Render](https://dashboard.render.com) → **New** → **Blueprint** (uses [`render.yaml`](render.yaml))  
   **or** **Web Service** pointing at the repo root:
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `python -u bot.py`
   - **Health Check Path:** `/health`
   - **Instance type:** Free
2. Environment variables:

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `MONGODB_URI` | recommended | Atlas URI |
| `MONGODB_DB` | no | Default `sudoku` |
| `DISCORD_GUILD_ID` | no | Server ID for instant slash sync |
| `PORT` | no | Render sets this automatically |

3. After deploy, open `https://YOUR-SERVICE.onrender.com/health` — should return `ok ready=True …`.
4. **UptimeRobot (importante no Free):** cria um monitor **HTTP(s)** a cada **5 minutos** para  
   `https://YOUR-SERVICE.onrender.com/health`  
   Sem isto, o Render Free desliga após ~15 min sem tráfego e o bot fica offline (`Esta interação falhou`).
5. Quando o Render estiver estável, podes pausar/apagar o serviço antigo no Fly.io.

`[`render.yaml`](render.yaml)` já define health check e `python -u bot.py`.

## Deploy Activity on Netlify

1. Import GitHub repo `joanafmaia/sudoku-squarepants` in [Netlify](https://app.netlify.com)
2. Build settings come from [`netlify.toml`](netlify.toml) (publish `activity/client/dist`)
3. **Environment variables** (Site settings):

| Variable | Scope | Description |
|---|---|---|
| `VITE_DISCORD_CLIENT_ID` | Build + Functions | OAuth2 application client ID |
| `DISCORD_CLIENT_SECRET` | Functions | OAuth2 client secret (never expose to the browser) |
| `MONGODB_URI` | Functions | Same Atlas URI as the bot |
| `MONGODB_DB` | Functions | Same DB name (e.g. `sudoku`) |

4. In Discord Developer Portal → your app → **OAuth2** → **Redirects**:
   - add `https://127.0.0.1` (placeholder required by the Embedded App SDK) → **Save**
5. In Discord Developer Portal → **Activities** → URL Mapping (sem `https://`):
   - `/` → `YOUR-SITE.netlify.app`
   - `/api` → `YOUR-SITE.netlify.app`  
     (com o mapeamento `/api`, o Discord remove o prefixo: `/.proxy/api/token` chega como `/token`)
6. **Activities → Settings** → Enable Activities
7. Atlas **Network Access**: allow Render + Netlify (often `0.0.0.0/0` for serverless).

Activity APIs:

- `POST /api/token` (also `/token`) — OAuth code → access_token
- `GET /api/leaderboard` (also `/leaderboard`) — top XP
- `POST /api/activity/win` (also `/activity/win`) — award solo Activity win

### Local Activity preview

```bash
# terminal 1 — optional OAuth helper (or use Netlify Dev)
cd activity/server && npm install && npm run dev

# terminal 2
cd activity/client && npm install && npm run dev
```

Copy [`activity/.env.example`](activity/.env.example) → `activity/.env` with `VITE_DISCORD_CLIENT_ID` (and secret for the local server).

## Environment (bot)

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `DISCORD_GUILD_ID` | no | Server ID for instant slash sync (`0` = global only) |
| `MONGODB_URI` | recommended | Atlas URI (in-memory fallback if unset) |
| `MONGODB_DB` | no | Database name (default `sudoku`) |
| `PORT` | no | Health HTTP port (Render / Fly set this) |

Never commit `.env` — it is gitignored.

## Optional: Fly.io

[`fly.toml`](fly.toml) still works if you prefer always-on without UptimeRobot (`fly deploy -a sudoku-squarepants`). Com Render Free + UptimeRobot não precisas do Fly.

## Commands

`/help` · `/play` · `/thcoku` · `/classic` · `/daily` · `/challenge` · `/shop` · `/quit` · `/leaderboard` · `/stats` · `/testboard`
