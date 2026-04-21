# S&P 500 Research Bot

A production-stable Telegram bot that ranks S&P 500 stocks using **only the last 7 calendar days** of price action, relative strength, volume, and news. Runs on [Railway](https://railway.app) as a single always-on process.

---

## Project Structure

```
spbot/
├── app/
│   ├── __init__.py       # package marker
│   ├── config.py         # all config from env vars
│   ├── storage.py        # SQLite + file cache helpers
│   ├── data_sources.py   # universe, prices, news
│   ├── research.py       # feature engineering + ranking
│   ├── ai.py             # optional OpenAI summaries
│   ├── notifiers.py      # Telegram + Pushover senders
│   └── main.py           # bot commands, scheduler, entry point
├── requirements.txt
├── run.sh
├── railway.json
├── .env.example
└── README.md
```

---

## Part 1 — Get Your Telegram Bot Token

### Step 1.1 — Create a bot with BotFather
1. Open Telegram on your iPhone.
2. Search for **@BotFather** and start a chat.
3. Send: `/newbot`
4. When prompted, enter a **name** (e.g. `My Stock Bot`) — this is just a display name.
5. When prompted, enter a **username** ending in `bot` (e.g. `my_sp500_bot`) — must be unique.
6. BotFather replies with your token: `1234567890:ABCDefghIJKlmnOPQRSTuvwXYZ`
   **Copy this. You will paste it into Railway as `TELEGRAM_BOT_TOKEN`.**

### Step 1.2 — Get your personal Chat ID
1. Search Telegram for **@userinfobot** and send `/start`.
2. It replies with your numeric ID, e.g. `987654321`.
   **Copy this. You will paste it into Railway as `TELEGRAM_CHAT_ID`.**

### Step 1.3 — Start a conversation with your bot
1. Search for your bot's username in Telegram.
2. Press **Start**. This is required before the bot can message you.

---

## Part 2 — Deploy to Railway

### Step 2.1 — Push your code to GitHub
```bash
cd spbot
git init
git add .
git commit -m "initial commit"
# Create a new repo on github.com (do NOT tick "Add a README")
git remote add origin https://github.com/YOUR_USERNAME/spbot.git
git push -u origin main
```

> ⚠️ Make sure `.env` is in `.gitignore`. **Never push real secrets to GitHub.**

### Step 2.2 — Create a Railway project
1. Go to [railway.app](https://railway.app) and sign in (GitHub login works).
2. Click **New Project**.
3. Choose **Deploy from GitHub repo**.
4. Select your `spbot` repository.
5. Railway detects `railway.json` and sets `bash run.sh` as the start command automatically.

### Step 2.3 — Set environment variables
1. In your Railway project, click on the **service** (the box that appeared).
2. Click the **Variables** tab.
3. Click **New Variable** and add each of the following:

| Variable | Required | Example value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | `1234567890:ABCDefghIJKlmnOPQRSTuvwXYZ` |
| `TELEGRAM_CHAT_ID` | Recommended | `987654321` |
| `OPENAI_API_KEY` | Optional | `sk-proj-...` |
| `MAX_UNIVERSE` | Optional | `30` |
| `DATA_BATCH_SIZE` | Optional | `10` |
| `BATCH_PAUSE_SECONDS` | Optional | `2.5` |
| `PRICE_CACHE_MINUTES` | Optional | `60` |
| `NEWS_CACHE_MINUTES` | Optional | `120` |
| `REPORT_CACHE_MINUTES` | Optional | `120` |
| `ENABLE_SCHEDULER` | Optional | `true` |
| `SCHEDULE_HOUR` | Optional | `7` |
| `SCHEDULE_MINUTE` | Optional | `30` |
| `SCHEDULE_TZ` | Optional | `America/New_York` |

4. After adding all variables, Railway redeploys automatically.

### Step 2.4 — Set replicas to exactly 1 (CRITICAL)
1. Click the **Settings** tab in your Railway service.
2. Find **Replicas** and set it to **1**.
3. If it is already 1, leave it. Never set it higher — this causes Telegram 409 Conflict errors.

### Step 2.5 — Verify the deployment
1. Click the **Deployments** tab and watch the build log.
2. A successful startup shows:
   ```
   ✓ Database ready
   ✓ Command handlers registered
   ✓ Scheduler started
   ✓ Bot is live — waiting for messages
   ```
3. Your bot sends `🤖 Bot started successfully.` to `TELEGRAM_CHAT_ID`.
4. Open Telegram, send `/help` to your bot — it should reply instantly.

### Step 2.6 — First data load
Send `/refresh` to your bot. It downloads prices and news for `MAX_UNIVERSE` tickers. This takes 1–3 minutes. After it completes, all other commands use cached data and respond in seconds.

---

## Part 3 — Available Commands

| Command | What it does |
|---|---|
| `/help` | Show all commands |
| `/status` | Health check, cache age, scheduler state |
| `/market` | 7-day market regime (SPY-based) |
| `/top10` | Top 10 ranked stocks by 7-day fresh signals |
| `/sectors` | Sector ETF performance (7d) |
| `/ticker NVDA` | Deep dive on any ticker |
| `/watch AAPL` | Add to personal watchlist |
| `/unwatch AAPL` | Remove from watchlist |
| `/watchlist` | Show watchlist with live scores |
| `/refresh` | Force full data rebuild |

---

## Part 4 — Troubleshooting

### ❌ Telegram 409 Conflict

**Symptom:** Railway logs show:
```
Telegram 409 Conflict — another bot instance is already polling
```

**Causes and fixes:**

| Cause | Fix |
|---|---|
| Railway replicas > 1 | Settings → Replicas → set to **1** |
| Two Railway deployments of same bot | Delete the older service in Railway |
| Running locally at the same time as Railway | Stop your local process |
| Previous deploy didn't fully stop | In Railway, click **Restart** to force a clean restart |

---

### ❌ Telegram 403 Forbidden / Unauthorized

**Symptom:** Logs show:
```
Telegram 403 Unauthorized — TELEGRAM_BOT_TOKEN is invalid
```

**Fixes:**
1. Go to Railway → Service → Variables → verify `TELEGRAM_BOT_TOKEN` value.
2. Copy the token directly from BotFather again (no extra spaces or quotes).
3. If you suspect the token is wrong, go to BotFather → `/mybots` → select your bot → **API Token** → **Revoke current token** → copy the new token → update Railway variable.
4. Redeploy after updating the variable.

---

### ❌ Railway build failure

**Symptom:** Build log shows `ModuleNotFoundError` or pip errors.

**Fixes:**
1. Check `requirements.txt` is in the repo root (same level as `railway.json`).
2. Check that all package versions exist on PyPI. Run locally:
   ```bash
   pip install -r requirements.txt
   ```
3. If a specific package fails, check [pypi.org](https://pypi.org) for the correct version number.
4. Railway NIXPACKS auto-detects Python from `requirements.txt`. No `Procfile` or `nixpacks.toml` needed.

---

### ❌ Missing module app.main

**Symptom:** Logs show:
```
ModuleNotFoundError: No module named 'app.main'
```
or
```
ModuleNotFoundError: No module named 'app'
```

**Fix:** The `app/` directory must have an `__init__.py` file. Verify it exists and is committed:
```bash
ls app/__init__.py   # should exist
git add app/__init__.py
git commit -m "add __init__.py"
git push
```

Also verify `railway.json` uses `bash run.sh` not `python app/main.py`.

---

### ❌ OpenAI API key exposed in GitHub (secret scanning)

**Symptom:** GitHub sends an email saying it detected an OpenAI API key.

**Fixes (do all steps in order):**
1. **Immediately rotate the key:**
   - Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
   - Click the compromised key → **Delete**
   - Click **Create new secret key** → copy the new key
2. **Update Railway:**
   - Railway → Service → Variables → update `OPENAI_API_KEY` → redeploy
3. **Remove from git history** (if you committed it):
   ```bash
   # Install BFG Repo Cleaner or use git-filter-repo
   git filter-repo --replace-text <(echo "OLD_KEY==>REMOVED")
   git push --force
   ```
4. **Add a pre-commit hook** to prevent future accidents:
   ```bash
   pip install pre-commit detect-secrets
   detect-secrets scan > .secrets.baseline
   pre-commit install
   ```
5. Add `.env` to `.gitignore`:
   ```
   echo ".env" >> .gitignore
   git add .gitignore && git commit -m "ignore .env"
   ```

---

### ❌ Yahoo Finance rate limits

**Symptom:** Logs show:
```
Yahoo rate-limit on batch — waiting 12.3s
```
or price data comes back empty.

**Fixes:**
1. **Increase `BATCH_PAUSE_SECONDS`** in Railway variables (try `5.0`).
2. **Decrease `DATA_BATCH_SIZE`** (try `5`).
3. **Decrease `MAX_UNIVERSE`** (try `20`) to reduce total request count.
4. Run `/refresh` at off-peak hours (early morning, before market open).
5. These are transient rate limits — the bot has automatic 3-attempt exponential back-off. If it keeps failing, wait 15 minutes and try `/refresh` again.

---

### ❌ Scheduler starts twice

**Symptom:** You see two `Scheduler started` log lines, or daily reports are sent twice.

**Root cause:** This only happens if `main()` is called twice in one process. The guard `_scheduler_started = True` prevents this within a single process. Between processes (Railway restarts), it's fine because each new process starts fresh.

**Fix:** Ensure Railway replicas = 1 and `ENABLE_SCHEDULER=true` (not duplicated in your start command). No other action needed — the code-level guard is already in place.

---

## Part 5 — Final Deployment Checklist

Work through this line by line before going live.

### Git & code
- [ ] `app/__init__.py` exists and is committed
- [ ] `.env` is in `.gitignore` and NOT committed
- [ ] `requirements.txt` is in the repo root
- [ ] `railway.json` is in the repo root
- [ ] `run.sh` is in the repo root and executable (`chmod +x run.sh`)
- [ ] All files pushed to GitHub: `git push origin main`

### Telegram setup
- [ ] Created bot with @BotFather
- [ ] Copied `TELEGRAM_BOT_TOKEN` (no quotes, no spaces)
- [ ] Found `TELEGRAM_CHAT_ID` via @userinfobot
- [ ] Sent `/start` to your bot in Telegram (required before it can message you)

### Railway configuration
- [ ] Created Railway project from your GitHub repo
- [ ] Added `TELEGRAM_BOT_TOKEN` to Variables
- [ ] Added `TELEGRAM_CHAT_ID` to Variables
- [ ] Set Replicas = 1 (Settings tab)
- [ ] Build completed successfully (green tick in Deployments tab)
- [ ] Startup message received in Telegram: "🤖 Bot started successfully"

### First run
- [ ] Sent `/help` — got reply within 3 seconds
- [ ] Sent `/refresh` — waited for completion message (1–3 min)
- [ ] Sent `/status` — shows last refresh time, scheduler running
- [ ] Sent `/top10` — received ranked list
- [ ] Sent `/market` — received regime summary
- [ ] Sent `/sectors` — received sector table
- [ ] Sent `/ticker AAPL` — received single-stock card
- [ ] Sent `/watch MSFT` then `/watchlist` — watchlist appears correctly

### Optional
- [ ] Added `OPENAI_API_KEY` to Variables (optional — bot works without it)
- [ ] Set `MAX_UNIVERSE=100` once stable (optional — start at 30)
- [ ] Configured Pushover keys for phone push notifications (optional)
- [ ] Adjusted `SCHEDULE_HOUR` and `SCHEDULE_TZ` for your timezone

---

## Part 6 — Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *required* | From @BotFather |
| `TELEGRAM_CHAT_ID` | `""` | Your Telegram numeric ID |
| `OPENAI_API_KEY` | `""` | Optional — enables AI summaries |
| `MAX_UNIVERSE` | `30` | Max S&P 500 tickers to process |
| `DATA_BATCH_SIZE` | `10` | Tickers per yfinance batch |
| `BATCH_PAUSE_SECONDS` | `2.5` | Pause between batches (rate limit buffer) |
| `PRICE_CACHE_MINUTES` | `60` | How long to reuse downloaded prices |
| `NEWS_CACHE_MINUTES` | `120` | How long to reuse news data |
| `REPORT_CACHE_MINUTES` | `120` | How long to reuse the ranking report |
| `UNIVERSE_CACHE_HOURS` | `24` | How long to cache the S&P 500 ticker list |
| `ENABLE_SCHEDULER` | `true` | Set to `false` to disable daily reports |
| `SCHEDULE_HOUR` | `7` | Hour for daily report (24h, local tz) |
| `SCHEDULE_MINUTE` | `30` | Minute for daily report |
| `SCHEDULE_TZ` | `America/New_York` | Timezone for scheduler |
| `DATA_DIR` | `/tmp/spbot_data` | Where to store caches and DB |
| `PUSHOVER_USER_KEY` | `""` | Optional Pushover user key |
| `PUSHOVER_APP_TOKEN` | `""` | Optional Pushover app token |

---

## Notes on the Research Engine

**What counts as a 7-day alpha signal (used for ranking):**
- 7-day, 5-day, 3-day, 1-day price return
- 7-day relative strength vs SPY
- Recent volume surge (5-day avg vs 20-day baseline)
- 7-day news intensity and sentiment tone

**What is treated as historical context only (not ranked):**
- Annualised historical volatility
- 20-day moving average position

**Composite score weights:**
- Momentum (7d/5d/3d/1d blended): 40%
- Relative strength vs SPY: 30%
- Volume surge: 15%
- News intensity × tone: 15%
