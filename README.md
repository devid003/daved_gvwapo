# Dabid Bots — Image Generator + Upload Times + USD/PHP (one bot, one dashboard)

One program runs the Discord bot **and** the web dashboard.

**Image generator**
- `/imagine`, `/autoimage start|stop|list`
- Server approval + member approval (waiting list), usage report with CSV export

**Upload-time reports (US audience)**
- Every 10 min: a separate BEST TIME TO UPLOAD report per niche (YTA: Commentary, Ranking, Distortion, Ranking + Commentary · FBA: AI Drama Pinoy, AI Drama English, Home Tips, Cooking, Ghibli-Style)
- Every hour: live USD ⇄ PHP exchange rate
- Once a day: today's best upload times
- `/report`, `/today`, `/rate`, `/niche`, `/hashtags`

**Dashboard pages:** Overview (servers + members) · Usage report · **Upload times** (channels per server, pause/resume, intervals, send now, today's schedule)

Everything is saved in the database (Postgres on Render), so nothing is lost on restart.

## Files
| File | What it does |
|---|---|
| `app.py` | Dashboard + starts the bot |
| `discord_bot.py` | Bot, approval checks, image commands, automations |
| `upload_reports.py` | Upload-time reports, exchange rate, their commands |
| `niches.py` | Best upload windows (Mon–Sun, ET) + hashtags — edit here |
| `db.py` | Database |
| `templates/` | Dashboard pages |

## Run on your PC
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then fill it in
python app.py               # open http://localhost:8000
```

## Deploy on Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
- **Health Check Path:** `/health`
- **Environment:** `PYTHON_VERSION=3.12.7`, `DISCORD_TOKEN`, `ADMIN_PASSWORD`, `SECRET_KEY`, `DATABASE_URL` (Neon/Supabase Postgres), `POLLINATIONS_TOKEN`, `OWNER_IDS`, `ALLOWED_GUILD_IDS`, `DISPLAY_TZ=Asia/Manila`
- Free plan sleeps after ~15 min without traffic: ping `https://YOUR-APP.onrender.com/health` every 5 min with UptimeRobot.
- If Discord answers 429 (shared Render IP temporarily blocked), the bot waits and retries automatically instead of crashing.

## Security
- Never put real values in `.env.example` or any file in the repo. Secrets go only in `.env` (ignored by git) or Render's Environment tab.
- Keep the GitHub repo **private**.
