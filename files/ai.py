"""
app/ai.py — Optional OpenAI-powered summaries.

All functions return None silently if USE_AI is False or the API call fails.
The bot works fully without this module being active.
"""
import logging
from typing import Optional

from app.config import OPENAI_API_KEY, USE_AI

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    """Lazily initialise the OpenAI client (avoids import-time crash if key missing)."""
    global _client
    if _client is not None:
        return _client
    if not USE_AI:
        return None
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialised (gpt-4o-mini)")
        return _client
    except Exception as exc:
        logger.error("OpenAI client init failed: %s", exc)
        return None


def _call(prompt: str, max_tokens: int = 250) -> Optional[str]:
    """Core wrapper: call gpt-4o-mini, return text or None."""
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("OpenAI API error: %s", exc)
        return None


# ── Public summarisers ────────────────────────────────────────────────────────

def summarize_top10(features_list: list[dict]) -> Optional[str]:
    """2-4 sentence theme summary of the top-10 ranked stocks."""
    if not features_list:
        return None
    top = features_list[:10]
    rows = "\n".join(
        f"{i+1}. {f['ticker']}: 7d={f['ret_7d']:+.1f}%  RS={f['rs_7d']:+.1f}%  "
        f"VolRatio={f['vol_ratio']:.1f}x  News={f['news_count']}"
        for i, f in enumerate(top)
    )
    prompt = (
        "You are a concise equity analyst assistant.\n"
        "Below are the top 10 S&P 500 stocks ranked by the last 7 days of "
        "price momentum, relative strength vs SPY, volume surge, and news flow.\n\n"
        f"{rows}\n\n"
        "Write 2-4 sentences summarising the dominant themes or sectors driving "
        "these names this week. Be factual, concise, suitable for a mobile reader. "
        "No disclaimers. No bullet points."
    )
    return _call(prompt, max_tokens=200)


def summarize_ticker(ticker: str, features: dict) -> Optional[str]:
    """2-3 sentence factual summary of a single ticker's recent behaviour."""
    if not features:
        return None
    prompt = (
        f"Stock: {ticker}\n"
        f"7-day return: {features['ret_7d']:+.1f}%\n"
        f"5-day return: {features['ret_5d']:+.1f}%\n"
        f"1-day return: {features['ret_1d']:+.1f}%\n"
        f"Relative strength vs SPY (7d): {features['rs_7d']:+.1f}%\n"
        f"Volume ratio (recent / hist avg): {features['vol_ratio']:.1f}x\n"
        f"News articles last 7 days: {features['news_count']}\n"
        f"Annualised historical volatility: {features['hist_vol']:.1f}%\n\n"
        "Write 2-3 sentences summarising this stock's recent behaviour. "
        "State whether it is outperforming or underperforming the market. "
        "Be factual, concise, no disclaimers."
    )
    return _call(prompt, max_tokens=150)


def summarize_market(regime: dict) -> Optional[str]:
    """2-3 sentence factual summary of current market conditions."""
    if not regime:
        return None
    prompt = (
        f"Market regime (SPY, last 7 days):\n"
        f"7-day return: {regime.get('ret_7d', 0):+.1f}%\n"
        f"5-day return: {regime.get('ret_5d', 0):+.1f}%\n"
        f"30-day annualised volatility: {regime.get('vol_30d', 0):.1f}%\n"
        f"SPY above 20-day MA: {regime.get('above_ma20', False)}\n"
        f"Label: {regime.get('label', 'Unknown')}\n\n"
        "Write 2-3 sentences summarising current market conditions. "
        "Be factual, concise, no disclaimers."
    )
    return _call(prompt, max_tokens=150)
