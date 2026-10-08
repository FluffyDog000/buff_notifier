"""The CSFloat bot's sales history, read-only.

The bot's collector writes the database all the time; we only read it. The
file is opened with `mode=ro`, so a stray write fails instead of touching the
bot's data, and WAL lets us read alongside the collector.

Tables used: `items(id, market_hash_name, active)` and
`sales(item_id, price, float_value, paint_seed, paint_index, sold_at)` -
price in USD, `sold_at` ISO-8601 UTC. A Doppler phase is its own item there,
already holding only that phase's sales.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def connect(path: str) -> sqlite3.Connection:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def active_items(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return [(r["id"], r["market_hash_name"]) for r in conn.execute(
        "SELECT id, market_hash_name FROM items WHERE active = 1 ORDER BY id")]


def item_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM items WHERE market_hash_name = ?", (name,)).fetchone()
    return row["id"] if row else None


def sales_for(conn: sqlite3.Connection, item_id: int, days: float,
              now: datetime | None = None, include_missing_float: bool = False) -> list[dict]:
    """Sales with a float in the last `days`, each with `age_days` attached -
    the shape `estimate` and `recency.to_today` read."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT price, float_value, paint_seed, paint_index, sold_at FROM sales "
        "WHERE item_id = ? " + ("" if include_missing_float else "AND float_value IS NOT NULL ") + "AND price > 0 "
        "AND sold_at >= ?", (item_id, cutoff))]
    for s in rows:
        try:
            t = datetime.fromisoformat(s["sold_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            s["age_days"] = (now - t).total_seconds() / 86400
        except (TypeError, ValueError):
            s["age_days"] = None
    return rows
