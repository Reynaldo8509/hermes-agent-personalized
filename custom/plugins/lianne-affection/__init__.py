"""Small, opt-in affection footer for display_one's Telegram conversations.

Blue Team note: identity is deny-by-default and uses the immutable Telegram
sender id, never a display name or message content. The counter is durable.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
display_one_TELEGRAM_ID = "8968754596"
STATE_DB = Path.home() / ".hermes" / "data" / "display_one-affection" / "counter.sqlite3"
PHRASES = (
    "✨ display_one, eres la parte más bonita de cualquier día.",
    "💛 display_one, tu sonrisa hace más amable hasta la pregunta más difícil.",
    "🌷 display_one, tienes una forma muy especial de iluminar todo a tu alrededor.",
)
_LOCAL = threading.local()


def _is_display_one(*, platform: object, sender_id: object) -> bool:
    return str(platform or "").lower() == "telegram" and str(sender_id or "") == display_one_TELEGRAM_ID


def _next_count() -> int:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STATE_DB, timeout=5) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("CREATE TABLE IF NOT EXISTS counter (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM counter WHERE name = 'display_one'").fetchone()
        count = int(row[0]) + 1 if row else 1
        conn.execute(
            "INSERT INTO counter(name, value) VALUES('display_one', ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (count,),
        )
        conn.commit()
    return count


def _pre_llm_call(**kwargs):
    """Count display_one's question and mark every fifth turn."""
    turn_id = str(kwargs.get("turn_id") or "")
    if not turn_id or not _is_display_one(platform=kwargs.get("platform"), sender_id=kwargs.get("sender_id")):
        return None
    seen = getattr(_LOCAL, "turns", None)
    if seen is None:
        seen = _LOCAL.turns = {}
    if turn_id not in seen:
        try:
            count = _next_count()
            seen[turn_id] = count if count % 5 == 0 else 0
        except Exception:
            logger.warning("display_one affection counter unavailable; skipping footer", exc_info=True)
            seen[turn_id] = 0
    return None


def _transform_llm_output(**kwargs):
    """Append a short footer only to the marked fifth display_one response."""
    turn_id = str(kwargs.get("turn_id") or "")
    count = getattr(_LOCAL, "turns", {}).pop(turn_id, 0)
    text = str(kwargs.get("response_text") or "").strip()
    if not count or not text or text.startswith("⚠️"):
        return None
    return f"{text}\n\n{PHRASES[(count // 5 - 1) % len(PHRASES)]}"


def register(ctx):
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
