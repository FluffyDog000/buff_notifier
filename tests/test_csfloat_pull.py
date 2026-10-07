"""The bot's database from its own server: a small, checked, atomic snapshot."""
import gzip
import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from notifier import sales
from notifier.poller import Poller
from notifier.store import Store
from tools.csfloat_export import export
from tools.pull_csfloat import install

NOW = datetime.now(timezone.utc)


def bot_db(path, recent=5, old=3):
    """The bot's own schema, extra columns and all."""
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, market_hash_name TEXT NOT NULL UNIQUE,
                            added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, last_polled_at TEXT);
        CREATE TABLE sales (sale_id TEXT PRIMARY KEY, item_id INTEGER NOT NULL, market_hash_name TEXT NOT NULL,
                            price_cents INTEGER, price REAL, float_value REAL, paint_seed INTEGER,
                            paint_index INTEGER, sold_at TEXT, sold_at_estimated INTEGER NOT NULL DEFAULT 0,
                            stickers_json TEXT, raw_json TEXT, scraped_at TEXT NOT NULL);""")
    w.execute("INSERT INTO items(id, market_hash_name, added_at, active) VALUES (1, 'AK', 'x', 1)")
    w.execute("INSERT INTO items(id, market_hash_name, added_at, active) VALUES (2, 'Old', 'x', 0)")
    rows = [(f"r{i}", 1, "AK", 0, 10.0 + i, 0.15, i, None, (NOW - timedelta(days=i)).isoformat(), 0,
             None, "{huge raw json}", "x") for i in range(recent)]
    rows += [(f"o{i}", 1, "AK", 0, 9.0, 0.15, i, None, (NOW - timedelta(days=90 + i)).isoformat(), 0,
              None, "{}", "x") for i in range(old)]
    w.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    w.commit()
    return w          # left open, as the running bot would hold it


def snapshot(src):
    buf = io.BytesIO()
    export(str(src), buf)
    return buf.getvalue()


def test_the_snapshot_holds_what_the_notifier_reads_and_no_more(tmp_path):
    src = tmp_path / "csfloat_sales.db"
    bot = bot_db(src)
    target = tmp_path / "data" / "csfloat_snapshot.db"
    items, n, last = install(snapshot(src), target)
    assert (items, n) == (1, 5), "60 days of sales; the inactive item counted out"
    conn = sales.connect(str(target))
    assert sales.active_items(conn) == [(1, "AK")]
    rows = sales.sales_for(conn, 1, 45)
    assert len(rows) == 5 and {"price", "float_value", "paint_seed", "age_days"} <= set(rows[0])
    bot.close()


def test_the_bots_database_is_left_as_it_was(tmp_path):
    src = tmp_path / "csfloat_sales.db"
    bot = bot_db(src)
    before = bot.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    snapshot(src)
    assert bot.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == before
    bot.close()


def test_a_bad_snapshot_never_replaces_a_good_one(tmp_path):
    src = tmp_path / "csfloat_sales.db"
    bot_db(src).close()
    target = tmp_path / "csfloat_snapshot.db"
    install(snapshot(src), target)
    good = target.read_bytes()
    with pytest.raises(Exception):
        install(b"not gzip", target)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).executescript(
        "CREATE TABLE items (id INTEGER, market_hash_name TEXT, active INTEGER);"
        "CREATE TABLE sales (item_id INTEGER, sold_at TEXT);")
    with pytest.raises(ValueError):
        install(gzip.compress(empty.read_bytes()), target)
    assert target.read_bytes() == good and not target.with_suffix(".tmp").exists()


def test_the_notifier_reopens_a_replaced_snapshot(tmp_path):
    src = tmp_path / "csfloat_sales.db"
    bot = bot_db(src, recent=5)
    target = tmp_path / "csfloat_snapshot.db"
    install(snapshot(src), target)
    p = Poller(Store(tmp_path / "buff.db"), tmp_path / "s.json", tmp_path / ".env")
    first = p.csfloat(str(target))
    assert p.csfloat(str(target)) is first, "same file, same connection"
    bot.execute("INSERT INTO sales VALUES ('new', 1, 'AK', 0, 50, 0.15, 1, NULL, ?, 0, NULL, '{}', 'x')",
                (NOW.isoformat(),))
    bot.commit()
    install(snapshot(src), target)
    again = p.csfloat(str(target))
    assert again is not first
    assert again.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 6
    bot.close()
