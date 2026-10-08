"""The CSFloat database is read, never written."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from notifier import sales
from notifier.estimate import estimate
from notifier.recency import to_today

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "csfloat_sales.db"
    w = sqlite3.connect(path)
    w.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, market_hash_name TEXT UNIQUE,
                            added_at TEXT, active INTEGER DEFAULT 1, last_polled_at TEXT);
        CREATE TABLE sales (sale_id TEXT PRIMARY KEY, item_id INTEGER, market_hash_name TEXT,
                            price_cents INTEGER, price REAL, float_value REAL, paint_seed INTEGER,
                            paint_index INTEGER, sold_at TEXT, scraped_at TEXT);
    """)
    w.execute("INSERT INTO items VALUES (1, 'AK-47 | Redline (Field-Tested)', '', 1, NULL)")
    w.execute("INSERT INTO items VALUES (2, 'Old Item', '', 0, NULL)")
    rows = [(f"s{i}", 1, "AK", 0, 20.0 + i % 3, 0.155, 100 + i, None,
             (NOW - timedelta(days=i)).isoformat(), "") for i in range(40)]
    rows.append(("nofloat", 1, "AK", 0, 99.0, None, None, None, NOW.isoformat(), ""))
    w.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    w.commit()
    w.close()
    return str(path)


def test_the_database_is_opened_read_only(db):
    conn = sales.connect(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sales")


def test_only_active_items_are_listed(db):
    conn = sales.connect(db)
    assert sales.active_items(conn) == [(1, "AK-47 | Redline (Field-Tested)")]
    assert sales.item_id(conn, "Old Item") == 2
    assert sales.item_id(conn, "Nope") is None


def test_sales_carry_their_age_and_feed_the_estimate(db):
    conn = sales.connect(db)
    rows = sales.sales_for(conn, 1, days=30, now=NOW)
    assert len(rows) == 31, "a month back, sales without a float left out"
    assert rows[0]["age_days"] == pytest.approx(0.0)
    assert max(r["age_days"] for r in rows) == pytest.approx(30.0)
    today, _ = to_today(rows, 30)
    price, basis = estimate(today, 0.1551)
    assert price == pytest.approx(21.0, abs=0.5) and "0.15" in basis


def test_vanilla_sales_can_include_missing_float(db):
    conn = sales.connect(db)
    rows = sales.sales_for(conn, 1, 30, NOW, include_missing_float=True)
    assert len(rows) == 32 and any(r['float_value'] is None for r in rows)
