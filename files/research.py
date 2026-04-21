"""
app/research.py — Feature engineering and ranking engine.

ALPHA RULE: The composite score uses ONLY data from the last 7 calendar days.
Historical data (>7 days) is used ONLY for:
  • annualised volatility (context)
  • 20-day MA position (context)
  • historical volume baseline
  • beta (not yet implemented — reserved for future)
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from app.config import REPORT_CACHE_MINUTES, REPORT_CACHE_PATH
from app.data_sources import (
    SECTOR_ETFS,
    fetch_all_news,
    load_sp500_universe,
    download_prices,
)
from app.storage import load_json_cache, save_json_cache

logger = logging.getLogger(__name__)

SECTOR_MAP: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
    "XLC": "Communication",
}

# ── Low-level helpers ─────────────────────────────────────────────────────────

def _close(df: pd.DataFrame) -> pd.Series:
    """Extract Close series from a yfinance DataFrame."""
    for col in ("Close", "Adj Close"):
        if col in df.columns:
            return df[col].dropna()
    # Try first numeric column as last resort
    for col in df.columns:
        try:
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) > 0:
                return s
        except Exception:
            pass
    return pd.Series(dtype=float)


def _volume(df: pd.DataFrame) -> pd.Series:
    """Extract Volume series."""
    if "Volume" in df.columns:
        return pd.to_numeric(df["Volume"], errors="coerce").dropna()
    return pd.Series(dtype=float)


def _ret_since_n_calendar_days(close: pd.Series, days: int) -> float:
    """
    Return the percentage price change over the last `days` calendar days.
    Uses the index timestamps to find the boundary accurately.
    Falls back to 0.0 on any error.
    """
    try:
        if close.empty:
            return 0.0
        # Normalise timezone
        idx = close.index
        if hasattr(idx, "tz_localize") and idx.tz is None:
            idx = idx.tz_localize("UTC")
        elif hasattr(idx, "tz_convert"):
            idx = idx.tz_convert("UTC")

        now_ts = pd.Timestamp.now(tz="UTC")
        cutoff = now_ts - pd.Timedelta(days=days)

        series_utc = close.copy()
        series_utc.index = idx

        before_cutoff = series_utc[series_utc.index < cutoff]
        after_cutoff = series_utc[series_utc.index >= cutoff]

        if after_cutoff.empty:
            return 0.0

        start_price = float(before_cutoff.iloc[-1]) if not before_cutoff.empty else float(after_cutoff.iloc[0])
        end_price = float(after_cutoff.iloc[-1])

        if start_price == 0:
            return 0.0
        return round((end_price - start_price) / start_price * 100, 4)
    except Exception as exc:
        logger.debug("_ret_since_n_calendar_days error: %s", exc)
        return 0.0


def _ret_last_n_bars(close: pd.Series, n: int) -> float:
    """
    Simpler fallback: % change using the last n rows (bars).
    Used when the index has no usable timezone info.
    """
    try:
        vals = close.dropna()
        if len(vals) < 2:
            return 0.0
        start = float(vals.iloc[max(0, len(vals) - n - 1)])
        end = float(vals.iloc[-1])
        if start == 0:
            return 0.0
        return round((end - start) / start * 100, 4)
    except Exception:
        return 0.0


def _safe_ret(close: pd.Series, calendar_days: int, bar_fallback: int) -> float:
    """Try calendar-day return; fall back to bar count if index has no tz."""
    r = _ret_since_n_calendar_days(close, calendar_days)
    if r == 0.0 and len(close) >= bar_fallback + 1:
        r = _ret_last_n_bars(close, bar_fallback)
    return r


def _volume_ratio(volume: pd.Series, n_recent: int = 5, n_hist: int = 20) -> float:
    """Recent avg volume / historical avg volume. > 1 means a surge."""
    try:
        v = volume.dropna()
        if len(v) < n_hist:
            return 1.0
        recent_avg = float(v.iloc[-n_recent:].mean())
        hist_avg = float(v.iloc[-n_hist:-n_recent].mean())
        if hist_avg == 0:
            return 1.0
        return round(recent_avg / hist_avg, 3)
    except Exception:
        return 1.0


def _news_signal(news_list: list) -> tuple[float, int]:
    """
    Score news activity over the last 7 days.
    Returns (score ∈ [0, 1], article_count).
    score = intensity × tone
    """
    if not news_list:
        return 0.0, 0

    cutoff = time.time() - 7 * 24 * 3600
    fresh = [n for n in news_list if n.get("providerPublishTime", 0) >= cutoff]
    count = len(fresh)
    if count == 0:
        return 0.0, 0

    positive_words = {
        "beat", "upgrade", "outperform", "buy", "surge", "rally", "gain",
        "record", "strong", "profit", "growth", "exceed", "raises", "bullish",
        "breakout", "positive", "expansion", "win", "soar", "jumps", "tops",
        "above", "guidance", "raised", "accelerating",
    }
    negative_words = {
        "miss", "downgrade", "underperform", "sell", "fall", "drop", "loss",
        "weak", "decline", "cut", "bearish", "warning", "risk", "concern",
        "lawsuit", "investigation", "layoff", "below", "shortfall", "plunge",
        "slump", "disappoints", "lowers", "lowered",
    }

    pos = neg = 0
    for article in fresh:
        title = (article.get("title") or "").lower()
        pos += sum(1 for w in positive_words if w in title)
        neg += sum(1 for w in negative_words if w in title)

    total_sentiment = pos + neg
    tone = (pos / total_sentiment) if total_sentiment > 0 else 0.5
    intensity = min(count / 5.0, 1.0)   # 5+ articles → max intensity
    score = round(intensity * (0.4 + 0.6 * tone), 4)
    return score, count


def _normalize(val: float, lo: float, hi: float) -> float:
    """Clip-normalize val ∈ [lo, hi] → [0, 1]."""
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (val - lo) / (hi - lo)))


# ── Per-ticker feature computation ───────────────────────────────────────────

def compute_features(
    ticker: str,
    price_data: dict[str, pd.DataFrame],
    news_data: dict[str, list],
    spy_prices: Optional[pd.DataFrame] = None,
) -> Optional[dict]:
    """
    Compute all features for a single ticker.
    Returns None if there is not enough price data.
    """
    df = price_data.get(ticker)
    if df is None or df.empty:
        return None

    close = _close(df)
    vol = _volume(df)

    if close.empty or len(close) < 3:
        return None

    # ── Fresh 7-day alpha signals ─────────────────────────────────────────────
    ret_7d = _safe_ret(close, 7, 5)
    ret_5d = _safe_ret(close, 5, 4)
    ret_3d = _safe_ret(close, 3, 3)
    ret_1d = _safe_ret(close, 1, 1)
    vol_ratio = _volume_ratio(vol, n_recent=5, n_hist=20)

    # Relative strength vs SPY (7-day calendar)
    rs_7d = 0.0
    if spy_prices is not None and not spy_prices.empty:
        spy_close = _close(spy_prices)
        spy_ret = _safe_ret(spy_close, 7, 5)
        rs_7d = round(ret_7d - spy_ret, 4)

    # News signal
    news_list = news_data.get(ticker, [])
    news_score, news_count = _news_signal(news_list)

    # ── Historical context (NOT part of alpha ranking) ────────────────────────
    hist_vol = 0.0
    above_ma20 = False
    try:
        if len(close) >= 20:
            returns = close.pct_change().dropna()
            hist_vol = round(float(returns.std() * (252 ** 0.5) * 100), 1)
            ma20 = float(close.rolling(20).mean().iloc[-1])
            above_ma20 = float(close.iloc[-1]) > ma20
    except Exception:
        pass

    # ── Composite score (fresh 7-day signals ONLY) ────────────────────────────
    # Weights: momentum 40%, RS vs market 30%, volume surge 15%, news 15%
    momentum_score = (
        0.40 * _normalize(ret_7d, -12, 12)
        + 0.30 * _normalize(ret_5d, -9, 9)
        + 0.20 * _normalize(ret_3d, -6, 6)
        + 0.10 * _normalize(ret_1d, -3, 3)
    )
    rs_score = _normalize(rs_7d, -10, 10)
    vol_score = _normalize(vol_ratio - 1.0, -0.5, 3.0)

    composite = round(
        0.40 * momentum_score
        + 0.30 * rs_score
        + 0.15 * vol_score
        + 0.15 * news_score,
        5,
    )

    return {
        "ticker": ticker,
        # ── fresh signals (alpha) ──
        "ret_7d": round(ret_7d, 2),
        "ret_5d": round(ret_5d, 2),
        "ret_3d": round(ret_3d, 2),
        "ret_1d": round(ret_1d, 2),
        "rs_7d": round(rs_7d, 2),
        "vol_ratio": round(vol_ratio, 2),
        "news_score": round(news_score, 3),
        "news_count": news_count,
        # ── historical context ──
        "hist_vol": hist_vol,
        "above_ma20": above_ma20,
        # ── ranking score ──
        "composite": composite,
    }


# ── Market regime ─────────────────────────────────────────────────────────────

def _compute_market_regime(prices: dict[str, pd.DataFrame]) -> dict:
    regime = {
        "ret_7d": 0.0,
        "ret_5d": 0.0,
        "vol_30d": 0.0,
        "above_ma20": False,
        "label": "Unknown",
    }
    spy = prices.get("SPY")
    if spy is None or spy.empty:
        logger.warning("SPY not available for regime calculation")
        return regime

    try:
        close = _close(spy)
        if close.empty:
            return regime

        ret_7d = _safe_ret(close, 7, 5)
        ret_5d = _safe_ret(close, 5, 4)

        # Annualised vol from last 20 bars
        returns = close.pct_change().dropna()
        vol_30d = 0.0
        if len(returns) >= 10:
            vol_30d = round(float(returns.iloc[-20:].std() * (252 ** 0.5) * 100), 1)

        # MA20
        above_ma20 = False
        if len(close) >= 20:
            ma20 = float(close.rolling(20).mean().iloc[-1])
            above_ma20 = float(close.iloc[-1]) > ma20

        # Regime label
        if ret_7d > 2.0:
            label = "🟢 Strong Bullish"
        elif ret_7d > 0.5:
            label = "🟢 Mild Bullish"
        elif ret_7d > -0.5:
            label = "🟡 Neutral"
        elif ret_7d > -2.0:
            label = "🟠 Mild Bearish"
        else:
            label = "🔴 Strong Bearish"

        if vol_30d > 25:
            label += " · High Vol"
        elif not above_ma20:
            label += " · Below MA20"

        regime.update({
            "ret_7d": round(ret_7d, 2),
            "ret_5d": round(ret_5d, 2),
            "vol_30d": vol_30d,
            "above_ma20": above_ma20,
            "label": label,
        })
    except Exception as exc:
        logger.error("_compute_market_regime error: %s", exc)

    return regime


# ── Sector performance ────────────────────────────────────────────────────────

def _compute_sector_performance(prices: dict[str, pd.DataFrame]) -> list[dict]:
    results = []
    for etf, name in SECTOR_MAP.items():
        df = prices.get(etf)
        if df is None or df.empty:
            logger.debug("Sector ETF %s not in price data", etf)
            continue
        close = _close(df)
        ret_7d = _safe_ret(close, 7, 5)
        results.append({"etf": etf, "name": name, "ret_7d": round(ret_7d, 2)})

    results.sort(key=lambda x: x["ret_7d"], reverse=True)
    return results


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(force: bool = False) -> dict:
    """
    Build the master ranking report.
    Reads from cache unless force=True.
    All /top10, /market, /sectors commands read from this report.
    """
    max_age = REPORT_CACHE_MINUTES * 60

    if not force:
        cached = load_json_cache(REPORT_CACHE_PATH, max_age)
        if cached:
            logger.info("Report from cache (generated %s)", cached.get("generated_at", "?")[:16])
            return cached

    logger.info("Building fresh research report…")

    tickers = load_sp500_universe()
    prices = download_prices(tickers, period="10d", force=force)
    news = fetch_all_news(tickers, force=force)

    spy_prices = prices.get("SPY")

    features_list: list[dict] = []
    skipped = 0
    for ticker in tickers:
        feat = compute_features(ticker, prices, news, spy_prices=spy_prices)
        if feat:
            features_list.append(feat)
        else:
            skipped += 1

    if skipped:
        logger.info("Skipped %d tickers with insufficient data", skipped)

    # Rank descending by composite score
    features_list.sort(key=lambda x: x["composite"], reverse=True)

    regime = _compute_market_regime(prices)
    sectors = _compute_sector_performance(prices)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(tickers),
        "ranked_count": len(features_list),
        "rankings": features_list,
        "market_regime": regime,
        "sectors": sectors,
    }

    save_json_cache(REPORT_CACHE_PATH, report)
    logger.info(
        "Report built: %d ranked, regime=%s, %d sectors",
        len(features_list), regime.get("label"), len(sectors),
    )
    return report


# ── Convenience accessors ─────────────────────────────────────────────────────

def get_top10(report: dict) -> list[dict]:
    return (report.get("rankings") or [])[:10]


def get_market_regime(report: dict) -> dict:
    return report.get("market_regime") or {}


def get_sectors(report: dict) -> list[dict]:
    return report.get("sectors") or []


def get_ticker_summary(ticker: str, report: dict) -> Optional[dict]:
    """
    Return ticker features from the cached report.
    If the ticker is not in the report, download it fresh (used for /ticker).
    """
    ticker = ticker.upper().strip()

    # Search existing report first (O(n) but n ≤ MAX_UNIVERSE, fast enough)
    for stock in report.get("rankings") or []:
        if stock.get("ticker") == ticker:
            return stock

    # Not found — download on-demand
    logger.info("Ticker %s not in report; downloading on-demand", ticker)
    try:
        prices = download_prices([ticker], period="10d", force=True)
        news = fetch_all_news([ticker], force=True)
        # Also fetch SPY for RS calculation
        spy_data = download_prices(["SPY"], period="10d", force=True)
        spy_prices = spy_data.get("SPY")
        return compute_features(ticker, prices, news, spy_prices=spy_prices)
    except Exception as exc:
        logger.error("get_ticker_summary on-demand error for %s: %s", ticker, exc)
        return None
