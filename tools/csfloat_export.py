#!/usr/bin/env python3
"""Runs ON THE CSFLOAT BOT'S SERVER: a small snapshot of its database to stdout.

    python3 csfloat_export.py /path/to/csfloat_sales.db  >  snapshot.db.gz

Only what the notifier reads: `items` and the last 60 days of `sales`, the
columns it uses. Read with `mode=ro` through SQLite itself, so a database the
bot is writing at that moment still gives a consistent copy, and nothing in
the bot's database changes.

Standard library only: it runs with the server's own python3, outside any
virtualenv. It is the forced command of the SSH key the notifier pulls with
(see docs/DEPLOY.md), so that key can do this and nothing else.
"""
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

DAYS = 60
SCHEMA = """
CREATE TABLE items (id INTEGER PRIMARY KEY, market_hash_name TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE sales (item_id INTEGER NOT NULL, market_hash_name TEXT, price REAL,
                    float_value REAL, paint_seed INTEGER, paint_index INTEGER, sold_at TEXT);
"""


def export(src_path: str, out) -> None:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        dst.executescript(SCHEMA)
        dst.executemany("INSERT INTO items VALUES (?,?,?)",
                        src.execute("SELECT id, market_hash_name, active FROM items"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
        cur = src.execute(
            "SELECT item_id, market_hash_name, price, float_value, paint_seed, paint_index, "
            "sold_at FROM sales WHERE sold_at >= ?", (cutoff,))
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            dst.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?)", rows)
        dst.execute("CREATE INDEX idx_sales_item_sold ON sales(item_id, sold_at)")
        dst.commit()
        dst.close()
        src.close()
        with open(tmp, "rb") as fh, gzip.GzipFile(fileobj=out, mode="wb") as gz:
            shutil.copyfileobj(fh, gz)
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: csfloat_export.py /path/to/csfloat_sales.db")
    export(sys.argv[1], sys.stdout.buffer)
