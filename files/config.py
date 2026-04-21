"""
app/config.py — All configuration loaded from environment variables.
No secrets are ever hardcoded here. All tuneable parameters have safe defaults.
"""
import os
import logging
from pathlib import Path

# python-dotenv is optional; if .env is present it will be loaded
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    """Raise a clear error if a required env var is missing."""
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"\n\n  ❌  Required environment variable '{name}' is not set.\n"
            f"      Add it to Railway → Service → Variables, then redeploy.\n"
        )
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ── Required ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# ── Optional but strongly recommended ───────────────────────────────────────
TELEGRAM_CHAT_ID: str = _optional("TELEGRAM_CHAT_ID")
OPENAI_API_KEY: str = _optional("OPENAI_API_KEY")
USE_AI: bool = bool(OPENAI_API_KEY)

# ── Universe / data settings ─────────────────────────────────────────────────
# MAX_UNIVERSE: how many S&P 500 tickers to process per run.
# Start at 30 to be conservative on a new deployment.
# You can safely raise this to 100+ once stable.
MAX_UNIVERSE: int = int(_optional("MAX_UNIVERSE", "30"))

# Batch size for yfinance downloads (avoid rate limits)
DATA_BATCH_SIZE: int = int(_optional("DATA_BATCH_SIZE", "10"))

# Seconds to pause between download batches
BATCH_PAUSE_SECONDS: float = float(_optional("BATCH_PAUSE_SECONDS", "2.5"))

# Cache TTLs
PRICE_CACHE_MINUTES: int = int(_optional("PRICE_CACHE_MINUTES", "60"))
NEWS_CACHE_MINUTES: int = int(_optional("NEWS_CACHE_MINUTES", "120"))
REPORT_CACHE_MINUTES: int = int(_optional("REPORT_CACHE_MINUTES", "120"))
UNIVERSE_CACHE_HOURS: int = int(_optional("UNIVERSE_CACHE_HOURS", "24"))

# ── Scheduler ────────────────────────────────────────────────────────────────
ENABLE_SCHEDULER: bool = _optional("ENABLE_SCHEDULER", "true").lower() == "true"
SCHEDULE_HOUR: int = int(_optional("SCHEDULE_HOUR", "7"))
SCHEDULE_MINUTE: int = int(_optional("SCHEDULE_MINUTE", "30"))
SCHEDULE_TZ: str = _optional("SCHEDULE_TZ", "America/New_York")

# ── Filesystem paths ──────────────────────────────────────────────────────────
# /tmp survives restarts within a Railway deployment session.
# For true persistence, mount a Railway volume and set DATA_DIR accordingly.
DATA_DIR: str = _optional("DATA_DIR", "/tmp/spbot_data")
DB_PATH: str = os.path.join(DATA_DIR, "spbot.db")
UNIVERSE_CACHE_PATH: str = os.path.join(DATA_DIR, "universe.json")
PRICE_CACHE_PATH: str = os.path.join(DATA_DIR, "price_cache.pkl")
NEWS_CACHE_PATH: str = os.path.join(DATA_DIR, "news_cache.json")
REPORT_CACHE_PATH: str = os.path.join(DATA_DIR, "report_cache.json")

# ── Optional Pushover notifications ──────────────────────────────────────────
PUSHOVER_USER_KEY: str = _optional("PUSHOVER_USER_KEY")
PUSHOVER_APP_TOKEN: str = _optional("PUSHOVER_APP_TOKEN")

# ── Bootstrap: ensure data directory exists ───────────────────────────────────
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

logger.debug(
    "Config loaded | MAX_UNIVERSE=%d | BATCH=%d | PAUSE=%.1fs | PRICE_TTL=%dm | "
    "NEWS_TTL=%dm | REPORT_TTL=%dm | USE_AI=%s | SCHEDULER=%s",
    MAX_UNIVERSE, DATA_BATCH_SIZE, BATCH_PAUSE_SECONDS,
    PRICE_CACHE_MINUTES, NEWS_CACHE_MINUTES, REPORT_CACHE_MINUTES,
    USE_AI, ENABLE_SCHEDULER,
)
