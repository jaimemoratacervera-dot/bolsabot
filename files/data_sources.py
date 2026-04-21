"""
app/data_sources.py — Data layer: S&P 500 universe, price downloads, news.

Design rules enforced here:
  • threads=False with yfinance (avoids thread-pool crashes on Railway)
  • Batched downloads with configurable size and pause
  • Exponential back-off on Yahoo rate-limit errors (HTTP 429)
  • Graceful fallback: one failed ticker or batch never kills the whole run
  • Local disk cache for all data; stale cache is better than nothing
"""
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from app.config import (
    BATCH_PAUSE_SECONDS,
    DATA_BATCH_SIZE,
    MAX_UNIVERSE,
    NEWS_CACHE_MINUTES,
    NEWS_CACHE_PATH,
    PRICE_CACHE_MINUTES,
    PRICE_CACHE_PATH,
    UNIVERSE_CACHE_HOURS,
    UNIVERSE_CACHE_PATH,
)
from app.storage import (
    load_json_cache,
    load_json_cache_stale,
    load_pickle_cache,
    save_json_cache,
    save_pickle_cache,
    set_state,
)

logger = logging.getLogger(__name__)

# Sector ETFs to download alongside the universe so /sectors works
SECTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLE", "XLI",
    "XLP", "XLY", "XLU", "XLRE", "XLB", "XLC",
]

# Wikipedia table ID for S&P 500 constituent list
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Hardcoded fallback universe — used only if Wikipedia fetch AND cache both fail
_FALLBACK_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "LLY",
    "JPM", "V", "XOM", "AVGO", "PG", "MA", "HD", "CVX", "MRK", "ABBV",
    "COST", "PEP", "KO", "BAC", "TMO", "CSCO", "ACN", "MCD", "CRM", "ABT",
]


# ── Universe ──────────────────────────────────────────────────────────────────

def load_sp500_universe(force: bool = False) -> list[str]:
    """
    Return up to MAX_UNIVERSE S&P 500 tickers.
    Source priority:
      1. Fresh disk cache (if not expired and not forced)
      2. Wikipedia HTML scrape
      3. Stale disk cache (any age)
      4. Hardcoded fallback list
    """
    max_age = UNIVERSE_CACHE_HOURS * 3600

    if not force:
        cached = load_json_cache(UNIVERSE_CACHE_PATH, max_age)
        if cached:
            tickers = cached[:MAX_UNIVERSE]
            logger.info("Universe from cache: %d tickers", len(tickers))
            return tickers

    logger.info("Fetching S&P 500 universe from Wikipedia…")
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(SP500_WIKI_URL, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        # Wikipedia uses id="constituents" on the main table
        table = soup.find("table", {"id": "constituents"})
        if table is None:
            # Fallback: grab first wikitable
            table = soup.find("table", {"class": "wikitable"})
        if table is None:
            raise ValueError("Could not locate S&P 500 table on Wikipedia page")

        tickers: list[str] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if cells:
                raw = cells[0].get_text(strip=True)
                # Wikipedia uses dots; yfinance uses dashes (e.g. BRK.B → BRK-B)
                ticker = raw.replace(".", "-")
                if ticker:
                    tickers.append(ticker)

        if len(tickers) < 400:
            raise ValueError(
                f"Wikipedia scrape returned only {len(tickers)} tickers (expected ~503)"
            )

        save_json_cache(UNIVERSE_CACHE_PATH, tickers)
        logger.info("Wikipedia: fetched %d tickers, saving to cache", len(tickers))
        return tickers[:MAX_UNIVERSE]

    except Exception as exc:
        logger.error("Wikipedia fetch failed: %s", exc)

    # Try stale cache
    stale = load_json_cache_stale(UNIVERSE_CACHE_PATH)
    if stale:
        logger.warning("Using stale universe cache: %d tickers", len(stale))
        return stale[:MAX_UNIVERSE]

    # Final hardcoded fallback
    logger.warning("Using hardcoded fallback universe (%d tickers)", len(_FALLBACK_UNIVERSE))
    return _FALLBACK_UNIVERSE[:MAX_UNIVERSE]


# ── Price download ─────────────────────────────────────────────────────────────

def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "rate limit", "too many requests", "yf.download"))


def _download_batch_raw(
    tickers: list[str],
    period: str = "10d",
    attempt: int = 0,
) -> dict[str, pd.DataFrame]:
    """
    Download one batch of tickers from yfinance.
    Returns {ticker: DataFrame} — failed tickers simply won't appear.
    """
    if not tickers:
        return {}

    results: dict[str, pd.DataFrame] = {}

    try:
        raw = yf.download(
            tickers,
            period=period,
            auto_adjust=True,
            threads=False,   # ← must be False to avoid Railway thread-pool crashes
            progress=False,
            timeout=30,
        )

        if raw is None or raw.empty:
            logger.warning("Empty yfinance response for batch: %s", tickers)
            return results

        # ── Multi-ticker download: columns are (field, ticker) MultiIndex ──
        if isinstance(raw.columns, pd.MultiIndex):
            for ticker in tickers:
                try:
                    df = raw.xs(ticker, level=1, axis=1).copy()
                    df.dropna(how="all", inplace=True)
                    if not df.empty and "Close" in df.columns:
                        results[ticker] = df
                except KeyError:
                    logger.debug("No data for %s in batch response", ticker)
        else:
            # Single ticker: columns are just (field,)
            if len(tickers) == 1:
                df = raw.copy()
                df.dropna(how="all", inplace=True)
                if not df.empty and "Close" in df.columns:
                    results[tickers[0]] = df

    except Exception as exc:
        if _is_rate_limit_error(exc):
            wait = (2 ** attempt) * 6 + random.uniform(1, 4)
            logger.warning(
                "Yahoo rate-limit on batch %s — waiting %.1fs (attempt %d)",
                tickers, wait, attempt + 1,
            )
            time.sleep(wait)
        else:
            logger.error("yfinance batch error: %s | tickers: %s", exc, tickers)

    return results


def _download_batch_with_retry(
    tickers: list[str],
    period: str = "10d",
) -> dict[str, pd.DataFrame]:
    """Up to 3 attempts with back-off."""
    for attempt in range(3):
        result = _download_batch_raw(tickers, period=period, attempt=attempt)
        if result:
            return result
        if attempt < 2:
            wait = (2 ** attempt) * 3 + random.uniform(0, 2)
            logger.warning("Retrying batch in %.1fs (attempt %d/3)", wait, attempt + 1)
            time.sleep(wait)
    return {}


def download_prices(
    tickers: list[str],
    period: str = "10d",
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Download prices for all tickers + SPY + sector ETFs.
    Batches downloads; caches result to disk.
    """
    max_age = PRICE_CACHE_MINUTES * 60
    if not force:
        cached = load_pickle_cache(PRICE_CACHE_PATH, max_age)
        if cached is not None:
            logger.info("Price data from cache (%d tickers)", len(cached))
            return cached

    # Build full list: universe + SPY + sectors (deduplicated)
    all_tickers = list(dict.fromkeys(tickers + ["SPY"] + SECTOR_ETFS))
    logger.info(
        "Downloading prices for %d tickers in batches of %d…",
        len(all_tickers), DATA_BATCH_SIZE,
    )

    all_data: dict[str, pd.DataFrame] = {}
    batches = [
        all_tickers[i: i + DATA_BATCH_SIZE]
        for i in range(0, len(all_tickers), DATA_BATCH_SIZE)
    ]

    for idx, batch in enumerate(batches):
        logger.info("Batch %d/%d: %s", idx + 1, len(batches), batch)
        batch_data = _download_batch_with_retry(batch, period=period)
        all_data.update(batch_data)
        if idx < len(batches) - 1:
            pause = BATCH_PAUSE_SECONDS + random.uniform(0, 1)
            logger.debug("Pausing %.1fs between batches", pause)
            time.sleep(pause)

    # Fallback: ensure SPY is present (needed for relative-strength calculation)
    if "SPY" not in all_data:
        logger.warning("SPY missing from batch results — fetching separately")
        spy_data = _download_batch_with_retry(["SPY"], period=period)
        all_data.update(spy_data)

    if not all_data:
        logger.error("Price download returned zero tickers")
    else:
        save_pickle_cache(PRICE_CACHE_PATH, all_data)
        set_state("last_price_refresh", datetime.now(timezone.utc).isoformat())
        logger.info("Price download complete: %d tickers", len(all_data))

    return all_data


# ── News ───────────────────────────────────────────────────────────────────────

def _fetch_news_single(ticker: str) -> list[dict]:
    """Fetch yfinance news for one ticker. Returns list of article dicts."""
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        cutoff = time.time() - 7 * 24 * 3600
        # Keep only articles from the last 7 days; strip heavy keys
        result = []
        for n in news:
            pub = n.get("providerPublishTime", 0)
            if pub >= cutoff:
                result.append({
                    "title": n.get("title", ""),
                    "publisher": n.get("publisher", ""),
                    "providerPublishTime": pub,
                    "link": n.get("link", ""),
                })
        return result
    except Exception as exc:
        logger.warning("yfinance news error for %s: %s", ticker, exc)
        return []


def fetch_all_news(
    tickers: list[str],
    force: bool = False,
) -> dict[str, list[dict]]:
    """
    Fetch 7-day news for every ticker. Caches result to disk.
    Returns {ticker: [article, ...]}
    """
    max_age = NEWS_CACHE_MINUTES * 60
    if not force:
        cached = load_json_cache(NEWS_CACHE_PATH, max_age)
        if cached is not None:
            logger.info("News from cache (%d tickers)", len(cached))
            return cached

    logger.info("Fetching news for %d tickers…", len(tickers))
    news_map: dict[str, list[dict]] = {}

    for i, ticker in enumerate(tickers):
        news_map[ticker] = _fetch_news_single(ticker)
        # Polite pause every 10 tickers
        if i > 0 and i % 10 == 0:
            time.sleep(1.0)

    total_articles = sum(len(v) for v in news_map.values())
    logger.info("News fetch done: %d articles across %d tickers", total_articles, len(tickers))
    save_json_cache(NEWS_CACHE_PATH, news_map)
    return news_map


# ── Orchestration ─────────────────────────────────────────────────────────────

def refresh_all_data(
    force: bool = True,
) -> tuple[list[str], dict[str, pd.DataFrame], dict[str, list[dict]]]:
    """
    Full refresh: universe → prices → news.
    Returns (tickers, price_dict, news_dict).
    """
    logger.info("refresh_all_data start (force=%s)", force)
    tickers = load_sp500_universe(force=force)
    prices = download_prices(tickers, period="10d", force=force)
    news = fetch_all_news(tickers, force=force)
    set_state("last_refresh", datetime.now(timezone.utc).isoformat())
    logger.info(
        "refresh_all_data done: %d tickers, %d price series, %d news series",
        len(tickers), len(prices), len(news),
    )
    return tickers, prices, news
