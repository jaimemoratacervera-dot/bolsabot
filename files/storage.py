"""
app/storage.py — SQLite for watchlist + bot state.
                 JSON and pickle helpers for file-based caches.
"""
import json
import logging
import os
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app.config import DATA_DIR, DB_PATH

logger = logging.getLogger(__name__)

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# ── SQLite helpers ────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safer for multi-thread access
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker      TEXT PRIMARY KEY,
                added_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS state (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  INTEGER NOT NULL
            );
        """)
        conn.commit()
        logger.debug("Database tables ensured")
    except Exception as exc:
        logger.error("init_db error: %s", exc)
    finally:
        conn.close()


def add_to_watchlist(ticker: str) -> bool:
    ticker = ticker.upper().strip()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
            (ticker, int(time.time())),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("add_to_watchlist(%s) error: %s", ticker, exc)
        return False
    finally:
        conn.close()


def remove_from_watchlist(ticker: str) -> bool:
    ticker = ticker.upper().strip()
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        logger.error("remove_from_watchlist(%s) error: %s", ticker, exc)
        return False
    finally:
        conn.close()


def get_watchlist() -> list[str]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT ticker FROM watchlist ORDER BY ticker"
        ).fetchall()
        return [r["ticker"] for r in rows]
    except Exception as exc:
        logger.error("get_watchlist error: %s", exc)
        return []
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )
        conn.commit()
    except Exception as exc:
        logger.error("set_state(%s) error: %s", key, exc)
    finally:
        conn.close()


def get_state(key: str) -> Optional[str]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception as exc:
        logger.error("get_state(%s) error: %s", key, exc)
        return None
    finally:
        conn.close()


# ── File cache helpers ────────────────────────────────────────────────────────

def save_json_cache(path: str, data: Any) -> None:
    """Persist data as JSON with a timestamp envelope."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"ts": time.time(), "data": data}, fh, default=str)
        os.replace(tmp, path)  # atomic on POSIX
    except Exception as exc:
        logger.error("save_json_cache(%s) error: %s", path, exc)


def load_json_cache(path: str, max_age_seconds: int) -> Optional[Any]:
    """Load JSON cache. Returns None if missing or expired."""
    try:
        with open(path) as fh:
            obj = json.load(fh)
        age = time.time() - obj.get("ts", 0)
        if age <= max_age_seconds:
            return obj["data"]
        logger.debug("Cache expired (%.0fs old, limit %ds): %s", age, max_age_seconds, path)
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error("load_json_cache(%s) error: %s", path, exc)
        return None


def load_json_cache_stale(path: str) -> Optional[Any]:
    """Load JSON cache ignoring expiry — used as last-resort fallback."""
    try:
        with open(path) as fh:
            obj = json.load(fh)
        return obj.get("data")
    except Exception:
        return None


def save_pickle_cache(path: str, data: Any) -> None:
    """Persist data as pickle with a timestamp envelope."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump({"ts": time.time(), "data": data}, fh)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("save_pickle_cache(%s) error: %s", path, exc)


def load_pickle_cache(path: str, max_age_seconds: int) -> Optional[Any]:
    """Load pickle cache. Returns None if missing or expired."""
    try:
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        age = time.time() - obj.get("ts", 0)
        if age <= max_age_seconds:
            return obj["data"]
        logger.debug("Pickle cache expired (%.0fs old): %s", age, path)
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error("load_pickle_cache(%s) error: %s", path, exc)
        return None
