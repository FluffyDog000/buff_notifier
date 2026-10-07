"""Bring a fresh snapshot of the CSFloat bot's database from its own server.

    .venv/bin/python -m tools.pull_csfloat            # run hourly by buff-pull.timer

The bot runs on another server. Its SSH key here (`/root/.ssh/csfloat_pull`)
is accepted there only as the forced command `csfloat_export.py`: connecting
with it hands back a gzipped snapshot and can do nothing else. The snapshot
is checked, then swapped in atomically as `data/csfloat_snapshot.db`; the
notifier notices the new file and reopens it.

The server is `csfloat_remote` in the settings (e.g. `root@46.8.237.170`).
"""
from __future__ import annotations

import gzip
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from notifier import config
from notifier.store import Store

SNAPSHOT = config.DATA / "csfloat_snapshot.db"
KEY = Path("/root/.ssh/csfloat_pull")


def check(path: Path) -> tuple[int, int, str | None]:
    """(active items, sales, latest sale) - or an exception for a bad file."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        items = conn.execute("SELECT COUNT(*) FROM items WHERE active = 1").fetchone()[0]
        sales, last = conn.execute("SELECT COUNT(*), MAX(sold_at) FROM sales").fetchone()
    finally:
        conn.close()
    if not items:
        raise ValueError("в копии нет ни одного предмета")
    return items, sales, last


def install(gz_bytes: bytes, target: Path = SNAPSHOT) -> tuple[int, int, str | None]:
    """Unpack, check, and atomically replace `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(gzip.decompress(gz_bytes))
    try:
        result = check(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    return result


def fetch(remote: str, key: Path = KEY, timeout: int = 600) -> bytes:
    r = subprocess.run(
        ["ssh", "-T", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20", remote],
        capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:]
                           or f"ssh: код {r.returncode}")
    return r.stdout


def main() -> int:
    s = config.load_settings()
    store = Store(config.STORE_PATH)
    remote = s.get("csfloat_remote", "").strip()
    if not remote:
        print("В настройках не задан сервер CSFloat-бота (csfloat_remote).")
        return 1
    try:
        items, sales, last = install(fetch(remote))
    except Exception as e:  # noqa: BLE001 - every failure is reported the same way
        store.set_status("csfloat_pull", f"ошибка: {e}")
        print(f"Не получилось: {e}")
        return 1
    if s["csfloat_db"] != str(SNAPSHOT) and not Path(s["csfloat_db"]).exists():
        config.save_settings({"csfloat_db": str(SNAPSHOT)})
    store.set_status("csfloat_pull", f"{items} предметов, {sales} продаж за 60 дней")
    print(f"Готово: {items} предметов, {sales} продаж, последняя {last}. Файл {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
