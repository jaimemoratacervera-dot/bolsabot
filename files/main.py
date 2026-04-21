"""
app/main.py — Entry point.

Architecture guarantees:
  • Exactly one Telegram getUpdates polling loop (via python-telegram-bot Updater)
  • Exactly one APScheduler BackgroundScheduler started once at boot
  • 409 Conflict → logged and process exits cleanly (Railway restarts it)
  • 403 Unauthorized → logged and process exits cleanly
  • No command handler may crash the bot; all errors are caught and replied
"""
import logging
import sys
import time
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot, ParseMode, Update
from telegram.error import Conflict, NetworkError, TelegramError, Unauthorized
from telegram.ext import CallbackContext, CommandHandler, Updater

from app.config import (
    ENABLE_SCHEDULER,
    SCHEDULE_HOUR,
    SCHEDULE_MINUTE,
    SCHEDULE_TZ,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from app.storage import (
    add_to_watchlist,
    get_state,
    get_watchlist,
    init_db,
    remove_from_watchlist,
)
from app.data_sources import load_sp500_universe, refresh_all_data
from app.research import (
    build_report,
    get_market_regime,
    get_sectors,
    get_ticker_summary,
    get_top10,
)
from app.ai import summarize_market, summarize_ticker, summarize_top10
from app.notifiers import send_pushover, send_telegram, split_and_send

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Suppress noisy third-party loggers
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)

# ── Scheduler singleton guards ────────────────────────────────────────────────
_scheduler: BackgroundScheduler | None = None
_scheduler_started: bool = False


# ── Shared helper ─────────────────────────────────────────────────────────────

def _safe_build_report(force: bool = False) -> dict:
    """Build report; return empty dict on any failure so commands don't crash."""
    try:
        return build_report(force=force)
    except Exception as exc:
        logger.error("build_report(%s) failed: %s", force, exc)
        return {}


def _fmt_ts(iso: str | None) -> str:
    """Format an ISO timestamp for display, e.g. '2024-06-10 07:30 UTC'."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso[:16]


# ── Command handlers ──────────────────────────────────────────────────────────
# Every handler wraps its body in try/except and replies with a friendly error
# message rather than letting the exception propagate to python-telegram-bot.

def cmd_start(update: Update, context: CallbackContext) -> None:  # noqa
    cmd_help(update, context)


def cmd_help(update: Update, context: CallbackContext) -> None:
    text = (
        "🤖 <b>S&amp;P 500 Research Bot</b>\n\n"
        "All signals use <b>only the last 7 calendar days</b> as the alpha source.\n\n"
        "<b>Commands</b>\n"
        "/status      — health check, cache info, scheduler state\n"
        "/market      — 7-day market regime summary\n"
        "/top10       — top 10 ranked S&amp;P 500 stocks\n"
        "/sectors     — sector ETF performance (7d)\n"
        "/ticker NVDA — single-stock deep dive\n"
        "/watch AAPL  — add to watchlist\n"
        "/unwatch AAPL — remove from watchlist\n"
        "/watchlist   — view watchlist with live scores\n"
        "/refresh     — force-rebuild all data &amp; report\n"
        "/help        — this message"
    )
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


def cmd_status(update: Update, context: CallbackContext) -> None:
    try:
        last_refresh = _fmt_ts(get_state("last_refresh"))
        last_price = _fmt_ts(get_state("last_price_refresh"))
        scheduler_ok = (
            _scheduler_started
            and _scheduler is not None
            and _scheduler.running
        )
        universe = load_sp500_universe()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        text = (
            f"✅ <b>Bot Status</b> — {now}\n\n"
            f"🕐 Last full refresh: <code>{last_refresh}</code>\n"
            f"📈 Last price pull:   <code>{last_price}</code>\n"
            f"📋 Universe size:     {len(universe)} tickers\n"
            f"⏰ Scheduler:         {'✅ running' if scheduler_ok else '⚠️ not running'}\n"
            f"📅 Daily report:      "
            f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}"
        )
        update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error("cmd_status error: %s", exc)
        update.message.reply_text(f"⚠️ Status error: {exc}")


def cmd_market(update: Update, context: CallbackContext) -> None:
    try:
        update.message.reply_text("🔄 Loading market data…")
        report = _safe_build_report(force=False)
        if not report:
            update.message.reply_text("⚠️ No data available yet. Run /refresh first.")
            return

        regime = get_market_regime(report)
        gen = report.get("generated_at", "")
        ai = summarize_market(regime)

        text = (
            f"🌐 <b>Market Regime (7-Day)</b>\n"
            f"<i>Report: {_fmt_ts(gen)}</i>\n\n"
            f"Status:           <b>{regime.get('label', 'Unknown')}</b>\n"
            f"SPY 7-day return: <b>{regime.get('ret_7d', 0):+.2f}%</b>\n"
            f"SPY 5-day return: {regime.get('ret_5d', 0):+.2f}%\n"
            f"Ann. vol (30d):   {regime.get('vol_30d', 0):.1f}%\n"
            f"Above 20-day MA:  {'✅ Yes' if regime.get('above_ma20') else '❌ No'}"
        )
        if ai:
            text += f"\n\n💡 <i>{ai}</i>"

        update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error("cmd_market error: %s", exc)
        update.message.reply_text(f"⚠️ Market command error: {exc}")


def cmd_top10(update: Update, context: CallbackContext) -> None:
    try:
        update.message.reply_text("🔄 Building top-10 list…")
        report = _safe_build_report(force=False)
        if not report:
            update.message.reply_text("⚠️ No data available yet. Run /refresh first.")
            return

        top = get_top10(report)
        if not top:
            update.message.reply_text("⚠️ Rankings not available. Run /refresh.")
            return

        gen = report.get("generated_at", "")
        ai = summarize_top10(top)

        lines = [
            "🏆 <b>Top 10 Ranked Stocks</b>",
            f"<i>Fresh 7-day signals only | {_fmt_ts(gen)}</i>\n",
        ]
        for i, f in enumerate(top, 1):
            vol_str = f"{f['vol_ratio']:.1f}× vol"
            news_str = f"{f['news_count']} news" if f["news_count"] else "no news"
            ma_str = "↑MA20" if f.get("above_ma20") else "↓MA20"
            lines.append(
                f"<b>{i:2}. {f['ticker']:<6}</b> "
                f"{f['ret_7d']:+.1f}% (7d) · "
                f"RS: {f['rs_7d']:+.1f}% · "
                f"{vol_str} · {news_str} · {ma_str}"
            )

        if ai:
            lines.append(f"\n💡 <i>{ai}</i>")
        lines.append("\n<i>Use /ticker SYMBOL for a deep dive</i>")

        split_and_send(str(update.message.chat_id), "\n".join(lines))
    except Exception as exc:
        logger.error("cmd_top10 error: %s", exc)
        update.message.reply_text(f"⚠️ Top-10 command error: {exc}")


def cmd_sectors(update: Update, context: CallbackContext) -> None:
    try:
        update.message.reply_text("🔄 Loading sector data…")
        report = _safe_build_report(force=False)
        if not report:
            update.message.reply_text("⚠️ No data available yet. Run /refresh first.")
            return

        sectors = get_sectors(report)
        if not sectors:
            update.message.reply_text(
                "⚠️ Sector data unavailable. Run /refresh to download sector ETFs."
            )
            return

        gen = report.get("generated_at", "")
        lines = [
            "📊 <b>Sector Performance (7-Day)</b>",
            f"<i>{_fmt_ts(gen)}</i>\n",
        ]
        for s in sectors:
            emoji = "🟢" if s["ret_7d"] >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>{s['name']:<22}</b> "
                f"({s['etf']})  {s['ret_7d']:+.2f}%"
            )

        update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error("cmd_sectors error: %s", exc)
        update.message.reply_text(f"⚠️ Sectors command error: {exc}")


def cmd_ticker(update: Update, context: CallbackContext) -> None:
    try:
        args = context.args or []
        if not args:
            update.message.reply_text(
                "Usage: /ticker SYMBOL\nExample: /ticker NVDA"
            )
            return

        ticker = args[0].upper().strip().replace(".", "-")
        update.message.reply_text(f"🔄 Looking up {ticker}…")

        report = _safe_build_report(force=False)
        feat = get_ticker_summary(ticker, report)

        if not feat:
            update.message.reply_text(
                f"⚠️ No data for <b>{ticker}</b>.\n"
                "Check the symbol or try /refresh.",
                parse_mode=ParseMode.HTML,
            )
            return

        ai = summarize_ticker(ticker, feat)

        text = (
            f"📈 <b>{ticker}</b>\n\n"
            "<b>── 7-Day Alpha Signals (fresh) ──</b>\n"
            f"  7d return:         <b>{feat['ret_7d']:+.2f}%</b>\n"
            f"  5d return:         {feat['ret_5d']:+.2f}%\n"
            f"  3d return:         {feat['ret_3d']:+.2f}%\n"
            f"  1d return:         {feat['ret_1d']:+.2f}%\n"
            f"  RS vs SPY (7d):    <b>{feat['rs_7d']:+.2f}%</b>\n"
            f"  Volume ratio:      {feat['vol_ratio']:.1f}× (recent / hist avg)\n"
            f"  News articles (7d):{feat['news_count']}\n\n"
            "<b>── Historical Context Only ──</b>\n"
            f"  Ann. volatility:   {feat['hist_vol']:.1f}%\n"
            f"  Above 20-day MA:   {'✅ Yes' if feat.get('above_ma20') else '❌ No'}\n\n"
            f"<b>Composite score:</b> {feat['composite']:.4f}"
        )
        if ai:
            text += f"\n\n💡 <i>{ai}</i>"

        update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error("cmd_ticker error: %s", exc)
        update.message.reply_text(f"⚠️ Ticker command error: {exc}")


def cmd_watch(update: Update, context: CallbackContext) -> None:
    try:
        args = context.args or []
        if not args:
            update.message.reply_text("Usage: /watch SYMBOL\nExample: /watch AAPL")
            return
        ticker = args[0].upper().strip().replace(".", "-")
        if add_to_watchlist(ticker):
            update.message.reply_text(
                f"✅ Added <b>{ticker}</b> to your watchlist.",
                parse_mode=ParseMode.HTML,
            )
        else:
            update.message.reply_text(f"⚠️ Failed to add {ticker}.")
    except Exception as exc:
        logger.error("cmd_watch error: %s", exc)
        update.message.reply_text(f"⚠️ Watch error: {exc}")


def cmd_unwatch(update: Update, context: CallbackContext) -> None:
    try:
        args = context.args or []
        if not args:
            update.message.reply_text("Usage: /unwatch SYMBOL\nExample: /unwatch AAPL")
            return
        ticker = args[0].upper().strip().replace(".", "-")
        if remove_from_watchlist(ticker):
            update.message.reply_text(
                f"✅ Removed <b>{ticker}</b> from your watchlist.",
                parse_mode=ParseMode.HTML,
            )
        else:
            update.message.reply_text(
                f"⚠️ {ticker} was not in your watchlist.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.error("cmd_unwatch error: %s", exc)
        update.message.reply_text(f"⚠️ Unwatch error: {exc}")


def cmd_watchlist(update: Update, context: CallbackContext) -> None:
    try:
        tickers = get_watchlist()
        if not tickers:
            update.message.reply_text(
                "Your watchlist is empty.\nUse /watch SYMBOL to add stocks."
            )
            return

        report = _safe_build_report(force=False)
        lines = [f"👀 <b>Watchlist</b> ({len(tickers)} stocks)\n"]
        for ticker in tickers:
            feat = get_ticker_summary(ticker, report)
            if feat:
                ma_str = "↑" if feat.get("above_ma20") else "↓"
                lines.append(
                    f"<b>{ticker:<6}</b>  "
                    f"{feat['ret_7d']:+.1f}% (7d)  "
                    f"RS: {feat['rs_7d']:+.1f}%  "
                    f"{ma_str}MA20"
                )
            else:
                lines.append(f"<b>{ticker}</b>  — (no data)")

        split_and_send(str(update.message.chat_id), "\n".join(lines))
    except Exception as exc:
        logger.error("cmd_watchlist error: %s", exc)
        update.message.reply_text(f"⚠️ Watchlist error: {exc}")


def cmd_refresh(update: Update, context: CallbackContext) -> None:
    try:
        update.message.reply_text(
            "🔄 Forcing full data refresh…\n"
            "This downloads prices and news for the entire universe. "
            "It may take 1–3 minutes depending on MAX_UNIVERSE."
        )
        tickers, prices, news = refresh_all_data(force=True)
        report = build_report(force=True)

        gen = report.get("generated_at", "")
        update.message.reply_text(
            f"✅ <b>Refresh complete!</b>\n\n"
            f"  Tickers in universe:  {len(tickers)}\n"
            f"  Price series loaded:  {len(prices)}\n"
            f"  Stocks ranked:        {report.get('ranked_count', 0)}\n"
            f"  Report generated:     {_fmt_ts(gen)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.error("cmd_refresh error: %s", exc)
        update.message.reply_text(f"⚠️ Refresh error: {exc}")


# ── Scheduled daily report ────────────────────────────────────────────────────

def _run_scheduled_report(bot: Bot) -> None:
    """
    Called by APScheduler every morning.
    Builds a fresh report and sends it to TELEGRAM_CHAT_ID.
    """
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set — skipping scheduled report")
        return

    logger.info("Running scheduled daily report…")
    try:
        report = build_report(force=True)
        top = get_top10(report)
        regime = get_market_regime(report)
        gen = report.get("generated_at", "")
        ai = summarize_top10(top)

        header = (
            f"📅 <b>Daily Report — {_fmt_ts(gen)[:10]}</b>\n"
            f"Market: {regime.get('label', '—')} | "
            f"SPY 7d: {regime.get('ret_7d', 0):+.2f}%\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )
        lines = [header]
        for i, f in enumerate(top, 1):
            lines.append(
                f"<b>{i:2}. {f['ticker']:<6}</b> "
                f"{f['ret_7d']:+.1f}% · RS: {f['rs_7d']:+.1f}%"
            )
        if ai:
            lines.append(f"\n💡 <i>{ai}</i>")

        split_and_send(TELEGRAM_CHAT_ID, "\n".join(lines))
        send_pushover(
            title="Daily Stock Report",
            message=f"Market: {regime.get('label')} | SPY 7d: {regime.get('ret_7d', 0):+.2f}%",
        )
        logger.info("Scheduled report delivered successfully")

    except Exception as exc:
        logger.error("Scheduled report failed: %s", exc)
        try:
            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ Scheduled report error: {exc}",
            )
        except Exception:
            pass


# ── Bot bootstrap ─────────────────────────────────────────────────────────────

def main() -> None:
    global _scheduler, _scheduler_started

    logger.info("=" * 60)
    logger.info("  S&P 500 Research Bot — starting up")
    logger.info("=" * 60)

    # 1. Initialise database
    init_db()
    logger.info("✓ Database ready")

    # 2. Build Updater — this is the ONE polling loop
    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    bot = updater.bot

    # 3. Register all commands
    dp.add_handler(CommandHandler("start",     cmd_start))
    dp.add_handler(CommandHandler("help",      cmd_help))
    dp.add_handler(CommandHandler("status",    cmd_status))
    dp.add_handler(CommandHandler("market",    cmd_market))
    dp.add_handler(CommandHandler("top10",     cmd_top10))
    dp.add_handler(CommandHandler("sectors",   cmd_sectors))
    dp.add_handler(CommandHandler("ticker",    cmd_ticker))
    dp.add_handler(CommandHandler("watch",     cmd_watch))
    dp.add_handler(CommandHandler("unwatch",   cmd_unwatch))
    dp.add_handler(CommandHandler("watchlist", cmd_watchlist))
    dp.add_handler(CommandHandler("refresh",   cmd_refresh))
    logger.info("✓ Command handlers registered")

    # 4. Start scheduler — guarded by _scheduler_started so it never runs twice
    if ENABLE_SCHEDULER and not _scheduler_started:
        try:
            tz = pytz.timezone(SCHEDULE_TZ)
            _scheduler = BackgroundScheduler(timezone=tz)
            _scheduler.add_job(
                _run_scheduled_report,
                CronTrigger(
                    hour=SCHEDULE_HOUR,
                    minute=SCHEDULE_MINUTE,
                    timezone=tz,
                ),
                id="daily_report",
                replace_existing=True,
                args=[bot],
            )
            _scheduler.start()
            _scheduler_started = True
            logger.info(
                "✓ Scheduler started — daily report at %02d:%02d %s",
                SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TZ,
            )
        except Exception as exc:
            logger.error("Scheduler startup failed: %s", exc)
    else:
        if not ENABLE_SCHEDULER:
            logger.info("Scheduler disabled (ENABLE_SCHEDULER=false)")

    # 5. Start polling — drop any stale updates so old commands don't replay
    logger.info("Starting Telegram long-polling…")
    try:
        updater.start_polling(
            poll_interval=1.0,
            timeout=20,
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
    except Conflict as exc:
        logger.critical(
            "❌  Telegram 409 Conflict — another bot instance is already polling.\n"
            "    On Railway: set Replicas = 1 in your service settings.\n"
            "    On local: kill any other running instance of this script.\n"
            "    Error: %s",
            exc,
        )
        sys.exit(1)
    except Unauthorized as exc:
        logger.critical(
            "❌  Telegram 403 Unauthorized — TELEGRAM_BOT_TOKEN is invalid.\n"
            "    Check the value in Railway → Service → Variables.\n"
            "    Error: %s",
            exc,
        )
        sys.exit(1)

    logger.info("✓ Bot is live — waiting for messages")

    # 6. Announce startup to the configured chat
    if TELEGRAM_CHAT_ID:
        try:
            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    "🤖 <b>Bot started successfully.</b>\n"
                    "Use /help to see available commands.\n"
                    "Use /refresh to load data for the first time."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Could not send startup message: %s", exc)

    # 7. Block main thread forever (Ctrl-C or SIGTERM will exit cleanly)
    updater.idle()

    # 8. Graceful shutdown
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
