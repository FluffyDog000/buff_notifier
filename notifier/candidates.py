"""Which of the bot's items are worth polling on BuffMarket.

The brief: liquid ones, where a listing below the market turns into money
quickly, and with a real premium for float. Polling every item the bot knows
(3000+) would ask BuffMarket thousands of times a day for items that sell
once a week.

For each active item, over the last 30 days of CSFloat sales:
    rate     sales a day
    median   the item's median price
    premium  how much the dearest hundredth of float (5+ sales) sells above
             the cheapest one - where the "float at an ordinary price" signal
             has room to fire
Ranked by rate. Pattern skins are left out when they are skipped anyway.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from .estimate import BUCKET_MIN_SALES
from .judge import is_pattern_skin

DAYS = 30


def pick(conn: sqlite3.Connection, s: dict, min_rate: float, min_price: float,
         limit: int, exclude: set[str] = frozenset(),
         now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=DAYS)).isoformat()
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT id, market_hash_name FROM items WHERE active = 1")}
    by_item: dict[int, list[tuple[float, float | None]]] = {}
    for item_id, price, f in conn.execute(
            "SELECT item_id, price, float_value FROM sales WHERE sold_at >= ? AND price > 0",
            (cutoff,)):
        if item_id in names:
            by_item.setdefault(item_id, []).append((price, f))

    out = []
    for item_id, rows in by_item.items():
        name = names[item_id]
        rate = len(rows) / DAYS
        if rate < min_rate or name in exclude:
            continue
        if s.get("pattern_skins") == "skip" and is_pattern_skin(name):
            continue
        median = statistics.median(p for p, _ in rows)
        if median < min_price:
            continue
        buckets: dict[int, list[float]] = {}
        for p, f in rows:
            if f is not None:
                buckets.setdefault(int(f * 100), []).append(p)
        meds = [statistics.median(v) for v in buckets.values() if len(v) >= BUCKET_MIN_SALES]
        premium = max(meds) / min(meds) - 1 if len(meds) >= 2 else None
        out.append({"name": name, "rate": rate, "median": median, "premium": premium})
    out.sort(key=lambda c: c["rate"], reverse=True)
    return out[:limit]
