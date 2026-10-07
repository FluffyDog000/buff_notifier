"""The notifier's own database (`data/buff.db`), shared with the web page.

    watch    - which BuffMarket items are polled: goods_id, the CSFloat name
               it is priced by, when it is next due, how the last poll went
    seen     - listing ids already read, to tell new ones from old (30 days)
    signals  - every alert ever made, one per listing - the "never twice"
    status   - small facts for the page: last cycle, session state, pause
    pending  - names waiting for their goods_id, found by the notifier between
               polls, one search at a time
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch (
    goods_id      INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,          -- CSFloat market_hash_name (phase name allowed)
    active        INTEGER NOT NULL DEFAULT 1,
    added_at      TEXT    NOT NULL,
    next_poll_at  TEXT,
    last_poll_at  TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    interval_min  REAL,
    sales_per_day REAL,
    polls         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (
    listing_id    TEXT PRIMARY KEY,
    goods_id      INTEGER NOT NULL,
    first_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_time ON seen(first_seen);
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    TEXT UNIQUE NOT NULL,
    goods_id      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- cheap | float
    price_usd     REAL,
    float_value   REAL,
    paint_seed    INTEGER,
    expected      REAL,                      -- CSFloat median the listing is priced by
    profit        REAL,                      -- after both fees, USD
    profit_pct    REAL,
    basis         TEXT,
    text          TEXT,
    created_at    TEXT NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (
    name          TEXT PRIMARY KEY,          -- CSFloat market_hash_name (phase name allowed)
    added_at      TEXT NOT NULL,
    tries         INTEGER NOT NULL DEFAULT 0,
    error         TEXT                       -- why the last search found nothing
);
CREATE TABLE IF NOT EXISTS status (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    at            TEXT
);
CREATE TABLE IF NOT EXISTS measurements (
    id            INTEGER PRIMARY KEY,
    signature     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    last_at       TEXT,
    interval_sec  REAL NOT NULL,
    proxy         INTEGER NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    poll_ok       INTEGER NOT NULL DEFAULT 0,
    search_ok     INTEGER NOT NULL DEFAULT 0,
    limited       INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    first_429_at  TEXT,
    before_429    INTEGER
);
CREATE TABLE IF NOT EXISTS request_events (
    id            INTEGER PRIMARY KEY,
    measurement_id INTEGER NOT NULL,
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    http_status   INTEGER,
    retry_after   REAL,
    pause_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_request_events_time ON request_events(at);
CREATE INDEX IF NOT EXISTS idx_request_events_measurement ON request_events(measurement_id, at);
CREATE TABLE IF NOT EXISTS work_claims (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    owner TEXT NOT NULL,
    until TEXT NOT NULL,
    PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS routes (
    key TEXT PRIMARY KEY,
    next_at TEXT,
    pause_until TEXT,
    owner TEXT,
    busy_until TEXT
);
CREATE TABLE IF NOT EXISTS route_aliases (
    alias TEXT PRIMARY KEY,
    target TEXT NOT NULL
);
"""


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        if "account_id" not in {r[1] for r in self.conn.execute("PRAGMA table_info(measurements)")}:
            try:
                self.conn.execute("ALTER TABLE measurements ADD COLUMN account_id TEXT NOT NULL DEFAULT 'primary'")
                self.conn.commit()
            except sqlite3.OperationalError:
                if "account_id" not in {r[1] for r in self.conn.execute("PRAGMA table_info(measurements)")}:
                    raise
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_measurement_account ON measurements(account_id, id)")

    # -- watch list ---------------------------------------------------------

    def add_watch(self, goods_id: int, name: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO watch(goods_id, name, added_at) VALUES (?,?,?) "
                "ON CONFLICT(goods_id) DO UPDATE SET name = excluded.name, active = 1",
                (goods_id, name.strip(), now_iso()))

    def remove_watch(self, goods_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM watch WHERE goods_id = ?", (goods_id,))

    def set_active(self, goods_id: int, active: bool) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE watch SET active = ? WHERE goods_id = ?",
                              (1 if active else 0, goods_id))

    def watch_list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM watch ORDER BY name")]

    def next_in_scan(self) -> dict | None:
        """Next active goods_id after the persisted cursor, wrapping at the end.

        Old per-item timers and sales rates have no effect on scanning.
        Reading the candidate does not move the cursor: a search may run first.
        """
        cursor = int(self.get_status("scan_cursor") or 0)
        row = self.conn.execute(
            "SELECT * FROM watch WHERE active=1 AND goods_id>? ORDER BY goods_id LIMIT 1",
            (cursor,)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT * FROM watch WHERE active=1 ORDER BY goods_id LIMIT 1").fetchone()
        return dict(row) if row else None

    def advance_scan(self, goods_id: int, now: datetime) -> None:
        """Persist progress before requesting; a failed item cannot hold up the list."""
        cursor = int(self.get_status("scan_cursor") or 0)
        started = self.get_status("scan_round_started")
        if not started or goods_id <= cursor:
            if started:
                self.set_status("scan_round_seconds", str(max(0, (now - datetime.fromisoformat(started)).total_seconds())))
            self.set_status("scan_round_started", now_iso(now))
        self.set_status("scan_cursor", str(goods_id))

    def claim_work(self, kind: str, owner: str, now: datetime) -> dict | None:
        """Atomically reserve the next job across workers and SQLite connections."""
        at, until = now_iso(now), now_iso(now + timedelta(seconds=120))
        with self.lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM work_claims WHERE until<=?", (at,))
            if kind == "poll":
                cursor_row = self.conn.execute("SELECT value FROM status WHERE key='scan_cursor'").fetchone()
                cursor = int(cursor_row[0] or 0) if cursor_row else 0
                sql = ("SELECT w.* FROM watch w WHERE active=1 AND NOT EXISTS "
                       "(SELECT 1 FROM work_claims c WHERE c.kind='poll' AND c.key=CAST(w.goods_id AS TEXT)) ")
                row = self.conn.execute(sql + "AND goods_id>? ORDER BY goods_id LIMIT 1", (cursor,)).fetchone()
                if row is None:
                    row = self.conn.execute(sql + "ORDER BY goods_id LIMIT 1").fetchone()
                if row is None:
                    return None
                key = str(row["goods_id"])
                started_row = self.conn.execute("SELECT value FROM status WHERE key='scan_round_started'").fetchone()
                started = started_row[0] if started_row else None
                updates = {"scan_cursor": key}
                if not started or row["goods_id"] <= cursor:
                    if started:
                        updates["scan_round_seconds"] = str(max(0, (now - datetime.fromisoformat(started)).total_seconds()))
                    updates["scan_round_started"] = at
                self.conn.executemany(
                    "INSERT INTO status(key,value,at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value,at=excluded.at", [(k, v, at) for k, v in updates.items()])
            else:
                row = self.conn.execute(
                    "SELECT p.* FROM pending p WHERE tries<3 AND NOT EXISTS "
                    "(SELECT 1 FROM work_claims c WHERE c.kind='search' AND c.key=p.name) "
                    "ORDER BY tries,added_at LIMIT 1").fetchone()
                if row is None:
                    return None
                key = row["name"]
            self.conn.execute("INSERT INTO work_claims(kind,key,owner,until) VALUES (?,?,?,?)",
                              (kind, key, owner, until))
            return dict(row)

    def release_work(self, kind: str, key: str, owner: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM work_claims WHERE kind=? AND key=? AND owner=?", (kind, str(key), owner))

    def acquire_route(self, key: str, owner: str, interval: float, now: datetime) -> bool:
        at = now.isoformat(timespec="microseconds")
        with self.lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            key = self._route_key(key)
            row = self.conn.execute("SELECT * FROM routes WHERE key=?", (key,)).fetchone()
            if row and any(row[k] and row[k] > at for k in ("next_at", "pause_until", "busy_until")):
                return False
            self.conn.execute(
                "INSERT INTO routes(key,next_at,owner,busy_until) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at,owner=excluded.owner,busy_until=excluded.busy_until",
                (key, (now + timedelta(seconds=interval)).isoformat(), owner,
                 now_iso(now + timedelta(seconds=120))))
            return True

    def release_route(self, key: str, owner: str) -> None:
        with self.lock, self.conn:
            key = self._route_key(key)
            self.conn.execute("UPDATE routes SET owner=NULL,busy_until=NULL WHERE key=? AND owner=?", (key, owner))

    def pause_route(self, key: str, until: str) -> None:
        with self.lock, self.conn:
            key = self._route_key(key)
            self.conn.execute(
                "INSERT INTO routes(key,pause_until) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                "pause_until=MAX(COALESCE(routes.pause_until,''),excluded.pause_until)", (key, until))

    def _route_key(self, key: str) -> str:
        row = self.conn.execute("SELECT target FROM route_aliases WHERE alias=?", (key,)).fetchone()
        return row[0] if row else key

    def route_pause(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT pause_until FROM routes WHERE key=?", (self._route_key(key),)).fetchone()
            return row[0] if row else None

    def link_routes(self, alias: str, target: str) -> None:
        """Retain pacing/cooldowns and in-flight ownership when an exit IP is learned."""
        if alias == target:
            return
        with self.lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            target = self._route_key(target)
            if self._route_key(alias) == target:
                return
            old = self.conn.execute("SELECT * FROM routes WHERE key=?", (alias,)).fetchone()
            current = self.conn.execute("SELECT * FROM routes WHERE key=?", (target,)).fetchone()
            if old:
                values = {k: max(old[k] or "", current[k] or "" if current else "") or None
                          for k in ("next_at", "pause_until", "busy_until")}
                owner = old["owner"] if not current or (old["busy_until"] or "") > (current["busy_until"] or "") else current["owner"]
                self.conn.execute("INSERT INTO routes(key,next_at,pause_until,owner,busy_until) VALUES (?,?,?,?,?) "
                                  "ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at,pause_until=excluded.pause_until,"
                                  "owner=excluded.owner,busy_until=excluded.busy_until",
                                  (target, values["next_at"], values["pause_until"], owner, values["busy_until"]))
                self.conn.execute("DELETE FROM routes WHERE key=?", (alias,))
            self.conn.execute("INSERT INTO route_aliases(alias,target) VALUES (?,?) "
                              "ON CONFLICT(alias) DO UPDATE SET target=excluded.target", (alias, target))

    def renew_claims(self, prefix: str, now: datetime) -> None:
        until = now_iso(now + timedelta(seconds=120))
        with self.lock, self.conn:
            self.conn.execute("UPDATE work_claims SET until=? WHERE owner LIKE ?", (until, prefix + "%"))
            self.conn.execute("UPDATE routes SET busy_until=? WHERE owner LIKE ?", (until, prefix + "%"))

    def polled(self, goods_id: int, now: datetime, interval_min: float,
               sales_per_day: float | None, error: str | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE watch SET last_poll_at = ?, next_poll_at = ?, interval_min = ?, "
                "sales_per_day = COALESCE(?, sales_per_day), last_error = ?, "
                "last_ok_at = CASE WHEN ? IS NULL THEN ? ELSE last_ok_at END, "
                "polls = polls + CASE WHEN ? IS NULL THEN 1 ELSE 0 END WHERE goods_id = ?",
                (now_iso(now), now_iso(now + timedelta(minutes=interval_min)), interval_min,
                 sales_per_day, error, error, now_iso(now), error, goods_id))

    # -- names waiting for a goods_id -----------------------------------------

    def add_pending(self, names) -> int:
        """Queue names not yet watched or queued. Returns how many were new."""
        watched = {w["name"] for w in self.watch_list()}
        fresh = [n.strip() for n in names if n.strip() and n.strip() not in watched]
        with self.lock, self.conn:
            before = self.conn.total_changes
            self.conn.executemany("INSERT OR IGNORE INTO pending(name, added_at) VALUES (?,?)",
                                  [(n, now_iso()) for n in fresh])
            return self.conn.total_changes - before

    def next_pending(self, max_tries: int = 3) -> dict | None:
        row = self.conn.execute("SELECT * FROM pending WHERE tries < ? ORDER BY tries, added_at "
                                "LIMIT 1", (max_tries,)).fetchone()
        return dict(row) if row else None

    def pending_list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM pending ORDER BY tries, added_at")]

    def resolve_pending(self, name: str, goods_id: int) -> None:
        self.add_watch(goods_id, name)
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM pending WHERE name = ?", (name,))

    def fail_pending(self, name: str, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE pending SET tries = tries + 1, error = ? WHERE name = ?",
                              (error, name))

    def remove_pending(self, name: str | None = None) -> None:
        """One name, or (None) every name that has given up."""
        with self.lock, self.conn:
            if name is None:
                self.conn.execute("DELETE FROM pending WHERE tries >= 3")
            else:
                self.conn.execute("DELETE FROM pending WHERE name = ?", (name,))

    # -- listings -------------------------------------------------------------

    def unseen(self, listing_ids: list[str]) -> set[str]:
        if not listing_ids:
            return set()
        marks = ",".join("?" * len(listing_ids))
        known = {r[0] for r in self.conn.execute(
            f"SELECT listing_id FROM seen WHERE listing_id IN ({marks})", listing_ids)}
        return set(listing_ids) - known

    def mark_seen(self, goods_id: int, listing_ids, now: datetime) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen(listing_id, goods_id, first_seen) VALUES (?,?,?)",
                [(i, goods_id, now_iso(now)) for i in listing_ids])

    def prune_seen(self, now: datetime, days: int = 30) -> int:
        with self.lock, self.conn:
            return self.conn.execute("DELETE FROM seen WHERE first_seen < ?",
                                     (now_iso(now - timedelta(days=days)),)).rowcount

    # -- signals --------------------------------------------------------------

    def signalled(self, listing_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM signals WHERE listing_id = ?",
                                 (listing_id,)).fetchone() is not None

    def add_signal(self, sig: dict) -> int | None:
        """Row id, or None when this listing already had its one alert."""
        cols = ("listing_id", "goods_id", "name", "kind", "price_usd", "float_value",
                "paint_seed", "expected", "profit", "profit_pct", "basis", "text", "created_at")
        with self.lock, self.conn:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO signals({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [sig.get(c) for c in cols])
            return cur.lastrowid if cur.rowcount else None

    def mark_sent(self, signal_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE signals SET sent = 1 WHERE id = ?", (signal_id,))

    def recent_signals(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))]

    # -- passive measurement of the notifier's requests ----------------------

    def measurement(self, signature: str, interval_sec: float, proxy: bool,
                    now: datetime, account_id: str = "primary") -> int:
        """Reuse the current run across restarts; changing its inputs starts a run.

        The signature is a digest. No cookies, proxy credentials or response
        bodies are stored here.
        """
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id, signature FROM measurements WHERE account_id=? ORDER BY id DESC LIMIT 1",
                                    (account_id,)).fetchone()
            if row and row["signature"] == signature:
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO measurements(signature, started_at, interval_sec, proxy, account_id) VALUES (?,?,?,?,?)",
                (signature, now_iso(now), interval_sec, int(proxy), account_id))
            return cur.lastrowid

    def record_request(self, measurement_id: int, now: datetime, kind: str,
                       outcome: str, http_status: int | None = None,
                       retry_after: float | None = None, pause_seconds: float | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO request_events(measurement_id, at, kind, outcome, http_status, "
                "retry_after, pause_seconds) VALUES (?,?,?,?,?,?,?)",
                (measurement_id, now_iso(now), kind, outcome, http_status, retry_after, pause_seconds))
            self.conn.execute(
                "UPDATE measurements SET last_at = ?, attempts = attempts + 1, "
                "poll_ok = poll_ok + ?, search_ok = search_ok + ?, limited = limited + ?, "
                "errors = errors + ?, "
                "before_429 = CASE WHEN ? AND first_429_at IS NULL THEN attempts ELSE before_429 END, "
                "first_429_at = CASE WHEN ? AND first_429_at IS NULL THEN ? ELSE first_429_at END "
                "WHERE id = ?",
                (now_iso(now), int(outcome == "ok" and kind == "poll"),
                 int(outcome == "ok" and kind == "search"), int(outcome == "limited"),
                 int(outcome not in ("ok", "limited")), outcome == "limited", outcome == "limited",
                 now_iso(now), measurement_id))

    def measurement_stats(self, now: datetime | None = None, account_id: str = "primary") -> dict:
        now = now or datetime.now(timezone.utc)
        row = self.conn.execute("SELECT * FROM measurements WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
        if row is None:
            return {"current": None, "history": [], "limits": []}
        current = dict(row)
        start = datetime.fromisoformat(current["started_at"])
        current["minutes"] = max(0, (now - start).total_seconds() / 60)
        current["first_429_minutes"] = ((datetime.fromisoformat(current["first_429_at"]) - start)
                                          .total_seconds() / 60 if current["first_429_at"] else None)
        hour = now_iso(max(start, now - timedelta(hours=1)))
        current["hour"] = dict(self.conn.execute(
            "SELECT COUNT(*) AS attempts, "
            "COALESCE(SUM(outcome='ok' AND kind='poll'),0) AS polls, "
            "COALESCE(SUM(outcome='ok' AND kind='search'),0) AS searches, "
            "COALESCE(SUM(outcome='limited'),0) AS limited "
            "FROM request_events WHERE measurement_id=? AND at>=? AND at<=?",
            (current["id"], hour, now_iso(now))).fetchone())
        return {"current": current,
                "history": [dict(r) for r in self.conn.execute(
                    "SELECT * FROM measurements WHERE account_id=? ORDER BY id DESC LIMIT 5", (account_id,))],
                "limits": [dict(r) for r in self.conn.execute(
                    "SELECT at, http_status, retry_after, pause_seconds FROM request_events "
                    "WHERE measurement_id=? AND outcome='limited' ORDER BY id DESC LIMIT 10",
                    (current["id"],))]}

    def prune_requests(self, now: datetime) -> None:
        """Keep seven days of individual events; cumulative runs remain intact."""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM request_events WHERE at < ?",
                              (now_iso(now - timedelta(days=7)),))

    def total_requests(self, now: datetime | None = None, minutes: float = 60) -> dict:
        now = now or datetime.now(timezone.utc)
        return dict(self.conn.execute(
            "SELECT COUNT(*) AS attempts, COALESCE(SUM(outcome='ok' AND kind='poll'),0) AS polls, "
            "COALESCE(SUM(outcome='ok' AND kind='search'),0) AS searches, "
            "COALESCE(SUM(outcome='limited'),0) AS limited FROM request_events WHERE at>=? AND at<=?",
            (now_iso(now - timedelta(minutes=minutes)), now_iso(now))).fetchone())

    # -- status ---------------------------------------------------------------

    def set_status(self, key: str, value: str | None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO status(key, value, at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, at = excluded.at",
                (key, value, now_iso()))

    def status(self) -> dict[str, dict]:
        return {r["key"]: {"value": r["value"], "at": r["at"]}
                for r in self.conn.execute("SELECT * FROM status")}

    def get_status(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM status WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
