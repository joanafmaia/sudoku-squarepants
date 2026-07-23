# Sudoku Squarepants (Thcoku)

Interactive 9×9 Sudoku for Discord — slash-command bot **and** Discord Activity, hosted together on **Render**.

## Architecture

| Piece | Host | Role |
|---|---|---|
| `bot.py` + `activity_http.py` | **Render** (1 Web Service) | Slash commands + Activity web UI + OAuth/Mongo APIs |
| Backup | **MongoDB Atlas** | Leaderboard / challenges / dailies / sessions |

```
Discord slash  ─┐
                ├→  Render (bot + Activity + /api/*)  →  MongoDB Atlas
Discord Activity┘
```

Free tip: keep the service awake with **UptimeRobot** → `https://YOUR-SERVICE.onrender.com/health` every 5 minutes.

## Features

- **`/play`** — opens the Discord Activity window (like Wordle); classic board → `/classic`
- **`/daily`** · **`/challenge`** · **`/shop`** · **`/leaderboard`** · **`/stats`**
- Activity wins write XP/sponges to the same Mongo leaderboard as the bot
- HTTP `/health` for Render + UptimeRobot

## Setup (local bot)

```bash
pip install -r requirements.txt
cp .env.example .env
# DISCORD_TOKEN, MONGODB_URI, optional Activity OAuth vars
python bot.py
```

Optional local Activity UI:

```bash
cd activity/client && npm install && npm run build
# bot serves activity/client/dist automatically when present
```

## Deploy everything on Render (Free)

One **Docker** Web Service builds the Activity and runs the bot.

1. Push repo to GitHub → [Render](https://dashboard.render.com) → **New** → **Blueprint** ([`render.yaml`](render.yaml))  
   or **Web Service** with **Language = Docker** (Dockerfile in repo root).
2. Environment variables:

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `MONGODB_URI` | recommended | Atlas URI |
| `MONGODB_DB` | no | Default `sudoku` |
| `DISCORD_GUILD_ID` | no | Server ID for instant slash sync (`0` = global) |
| `VITE_DISCORD_CLIENT_ID` | yes (Activity) | OAuth2 application client ID (**needed at Docker build**) |
| `DISCORD_CLIENT_SECRET` | yes (Activity) | OAuth2 client secret (runtime) |

3. Health Check Path: `/health`
4. After deploy: `https://YOUR-SERVICE.onrender.com/health` → `ok ready=True …`
5. **UptimeRobot:** HTTP monitor every **5 min** on that `/health` URL
6. Discord Developer Portal:
   - **OAuth2 → Redirects:** `https://127.0.0.1`
   - **Activities → URL Mappings** (sem `https://`):
     - `/` → `YOUR-SERVICE.onrender.com`
     - `/api` → `YOUR-SERVICE.onrender.com`
   - **Activities → Settings** → Enable Activities
7. Suspend **Netlify** and **Fly** when Render is stable (one token / one host).

If you already created a **Python** service, change it to **Docker** (or recreate from Blueprint) so the Activity build runs.

### APIs (same host)

- `POST /api/token` (also `/token`)
- `GET /api/leaderboard` (also `/leaderboard`)
- `POST /api/activity/win` (also `/activity/win`)

## Environment

Never commit `.env`.

| Variable | Where |
|---|---|
| `DISCORD_TOKEN` | Render |
| `MONGODB_URI` / `MONGODB_DB` | Render |
| `DISCORD_GUILD_ID` | Render (optional) |
| `VITE_DISCORD_CLIENT_ID` | Render (build + runtime) |
| `DISCORD_CLIENT_SECRET` | Render (runtime only) |

## Commands

`/help` · `/play` · `/thcoku` · `/classic` · `/daily` · `/challenge` · `/shop` · `/quit` · `/leaderboard` · `/stats` · `/testboard`
