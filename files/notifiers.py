"""
app/notifiers.py — Outbound notification helpers.

  • send_telegram()      — single message via Bot API (direct HTTP, no polling)
  • split_and_send()    — chunk-aware sender for long reports
  • send_pushover()     — optional Pushover push notification

All functions are safe to call at any time; they log errors and return
gracefully rather than raising exceptions.
"""
import logging
import time
from typing import Optional

import requests

from app.config import PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

# Telegram hard limit is 4096 UTF-8 characters per message.
# We use 4000 to leave headroom for multi-byte characters and formatting tags.
_MAX_TG_CHARS = 4000


def send_telegram(
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
) -> bool:
    """
    Send a single Telegram message via Bot API.
    Returns True on success, False on any failure.
    Does NOT raise exceptions.
    """
    if not chat_id:
        logger.warning("send_telegram called with empty chat_id — skipping")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        # Log the Telegram error code and description
        body = resp.json() if resp.content else {}
        logger.error(
            "Telegram API error %d: %s",
            resp.status_code,
            body.get("description", resp.text[:200]),
        )
        return False
    except requests.exceptions.Timeout:
        logger.error("Telegram send timed out for chat_id=%s", chat_id)
        return False
    except Exception as exc:
        logger.error("Telegram network error: %s", exc)
        return False


def split_and_send(
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    inter_message_pause: float = 0.35,
) -> None:
    """
    Send `text` to Telegram, splitting into multiple messages if it exceeds
    the 4000-character limit. Splits on newlines to avoid mid-line breaks.
    """
    if len(text) <= _MAX_TG_CHARS:
        send_telegram(chat_id, text, parse_mode)
        return

    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = (chunk + "\n" + line).lstrip("\n")
        if len(candidate) > _MAX_TG_CHARS:
            if chunk:
                send_telegram(chat_id, chunk, parse_mode)
                time.sleep(inter_message_pause)
            chunk = line
        else:
            chunk = candidate

    if chunk.strip():
        send_telegram(chat_id, chunk, parse_mode)


def send_pushover(title: str, message: str) -> bool:
    """
    Send a Pushover notification.
    Returns False silently if Pushover keys are not configured.
    """
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        return False
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title[:250],
                "message": message[:1024],
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Pushover error %d: %s", resp.status_code, resp.text[:100])
        return False
    except Exception as exc:
        logger.error("Pushover send error: %s", exc)
        return False
