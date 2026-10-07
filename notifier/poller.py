"""Continuous round-robin scanning of all active items, one request at a time.

Each cycle re-reads settings and `.env`, so whatever the web page changes
applies on the next item. Sales on CSFloat inform valuation, never how
long an item waits. The persisted cursor visits every active item and
starts the next round immediately. The client's request pause sets the pace.

Stops asking BuffMarket, and says so in Telegram once:
- the session has expired (`Login Required`) - until a new one is saved;
- 429 - for as long as the site asks, 15 minutes when it does not say.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import buff, config, phases, sales
from .buff import BuffError, LoginRequired
from .judge import HISTORY_DAYS, Market, judge, message
from .listings import item_url, parse_page
from .store import Store, now_iso
from .telegram import Telegram

log = logging.getLogger("buff.poller")

RATE_DAYS = 30.0
BACKOFF_429 = 15 * 60
IDLE = 30.0
SEARCH_EVERY = 4
TARGET_CYCLE_MINUTES = 5.0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Poller:
    def __init__(self, store: Store, settings_path: Path = config.SETTINGS_PATH,
                 env_path: Path = config.ENV_PATH, client_factory=buff.from_config,
                 telegram_factory=Telegram, connect=sales.connect, work_owner: str | None = None):
        self.store = store
        self.settings_path, self.env_path = settings_path, env_path
        self.client_factory, self.telegram_factory, self.connect = \
            client_factory, telegram_factory, connect
        self._client, self._client_key = None, None
        self._db, self._db_path = None, None
        self._pruned: datetime | None = None
        self._polls_in_a_row = 0
        self._request_finished_at: datetime | None = None
        self.work_owner = work_owner

    def context(self) -> tuple[dict, dict]:
        return config.load_settings(self.settings_path), config.load_secrets(self.env_path)

    # -- helpers --------------------------------------------------------------

    def client(self, s: dict, sec: dict):
        key = buff.fingerprint(s, sec)
        if self._client is None or key != self._client_key:
            self._client, self._client_key = self.client_factory(s, sec), key
        return self._client

    def csfloat(self, path: str) -> sqlite3.Connection:
        """The bot's database, reopened when the path or the file itself changes:
        a fresh snapshot replaces the file, and an open connection would go on
        reading the old one."""
        st = os.stat(path)          # FileNotFoundError when it is not there
        ident = (path, st.st_ino, st.st_dev)
        if self._db is None or ident != self._db_path:
            if self._db is not None:
                self._db.close()
            self._db, self._db_path = self.connect(path), ident
        return self._db

    def alert_once(self, tg: Telegram, key: str, marker: str, text: str) -> None:
        """Send `text` unless this `marker` was already alerted under `key`."""
        if self.store.get_status(key) == marker:
            return
        if tg.send(text) or not tg.configured():
            self.store.set_status(key, marker)

    def request(self, kind: str, s: dict, sec: dict, now: datetime, **params) -> dict:
        """Observe normal polling/searching, without making additional requests."""
        signature = _hash("round-robin-v1:" + json.dumps(buff.fingerprint(s, sec), ensure_ascii=True))
        mid = self.store.measurement(signature, s["request_interval"], bool(sec["BUFF_PROXY"]), now)
        client = self.client(s, sec)
        started = time.monotonic()
        self._request_finished_at = None

        def record(outcome, status=None, retry=None, pause=None):
            self._request_finished_at = now + timedelta(seconds=time.monotonic() - started)
            self.store.record_request(mid, self._request_finished_at, kind, outcome, status, retry, pause)

        try:
            body = (client.sell_orders(params["goods_id"], page_size=params["page_size"])
                    if kind == "poll" else client.search_goods(params["query"]))
        except LoginRequired as e:
            record("expired", e.status)
            raise
        except BuffError as e:
            limited = e.status == 429
            record("limited" if limited else "error", e.status, e.retry_after,
                   (e.retry_after if e.retry_after is not None else BACKOFF_429) if limited else None)
            raise
        except requests.RequestException:
            record("network_error")
            raise
        record("ok", 200)
        return body

    # -- one cycle ------------------------------------------------------------

    def cycle(self, now: datetime | None = None) -> float:
        """Poll at most one item. Returns seconds to wait before the next cycle."""
        now = now or datetime.now(timezone.utc)
        s, sec = self.context()
        tg = self.telegram_factory(sec["TELEGRAM_BOT_TOKEN"], sec["TELEGRAM_CHAT_ID"])
        self.store.set_status("heartbeat", now_iso(now))

        if s["paused"]:
            self.store.set_status("state", "пауза (настройки)")
            return IDLE
        if not sec["BUFF_COOKIE"]:
            self.store.set_status("state", "нет сессии BuffMarket")
            self.store.set_status("session", "нет")
            return IDLE
        cookie = _hash(sec["BUFF_COOKIE"])
        if self.store.get_status("session_expired") == cookie:
            self.store.set_status("state", "сессия истекла, ждём новую")
            return IDLE
        until = self.store.get_status("pause_until")
        if until and datetime.fromisoformat(until) > now:
            self.store.set_status("state", f"пауза после 429 до {until[11:16]} UTC")
            return min(IDLE, (datetime.fromisoformat(until) - now).total_seconds())

        try:
            db = self.csfloat(s["csfloat_db"])
        except (OSError, sqlite3.Error) as e:
            self.store.set_status("state", f"база CSFloat недоступна: {e}")
            return 60.0

        item = self.store.next_in_scan()
        pending = self.store.next_pending()
        # One search after four polls, or all requests when the watch list is empty.
        if pending and (item is None or self._polls_in_a_row >= SEARCH_EVERY):
            if self.work_owner:
                pending = self.store.claim_work("search", self.work_owner, now)
            if pending:
                self._polls_in_a_row = 0
                self.store.set_status("state", "работает: ищу goods_id")
                try:
                    return self.resolve(pending, s, sec, tg, now, cookie)
                finally:
                    if self.work_owner:
                        self.store.release_work("search", pending["name"], self.work_owner)
        if item is None:
            self.store.set_status("state", "работает")
            return IDLE

        self.prune(now)
        if self.work_owner:
            item = self.store.claim_work("poll", self.work_owner, now)
            if item is None:
                return 1.0
        self._polls_in_a_row += 1
        if not self.work_owner:
            self.store.advance_scan(item["goods_id"], now)
        self.store.set_status("state", "работает: обход по кругу")
        try:
            return self.poll(item, s, sec, db, tg, now, cookie)
        finally:
            if self.work_owner:
                self.store.release_work("poll", str(item["goods_id"]), self.work_owner)

    def poll(self, item: dict, s: dict, sec: dict, db, tg: Telegram, now: datetime,
             cookie: str) -> float:
        gid, name = item["goods_id"], item["name"]
        try:
            body = self.request("poll", s, sec, now, goods_id=gid, page_size=s["page_size"])
        except LoginRequired:
            self.session_expired(tg, cookie)
            return 0.0
        except BuffError as e:
            self.maybe_429(e, tg, self._request_finished_at or now)
            self.store.polled(gid, now, 0, None, str(e))
            return 0.0
        except requests.RequestException as e:
            self.store.polled(gid, now, 0, None, f"сеть: {type(e).__name__}")
            return 0.0

        self.store.set_status("session", "работает")
        page = parse_page(body)
        ids = [x.id for x in page.listings]
        new = self.store.unseen(ids)
        gap = item["polls"] > 0 and len(ids) >= s["page_size"] and new == set(ids)

        item_id = sales.item_id(db, name)
        if item_id is None:
            self.store.mark_seen(gid, ids, now)
            self.store.polled(gid, now, 0, None,
                              "нет такого предмета в базе CSFloat")
            return 0.0
        rows = sales.sales_for(db, item_id, HISTORY_DAYS, now)
        rate = sum(1 for r in rows
                   if r["age_days"] is not None and r["age_days"] <= RATE_DAYS) / RATE_DAYS
        market = Market.build(rows, s["window_days"])

        sent = 0
        for x in page.listings:
            if self.store.signalled(x.id):
                continue
            v = judge(x, name, market, s)
            if not v.kind:
                continue
            text = message(x, name, v, item_url(name))
            sid = self.store.add_signal(dict(
                listing_id=x.id, goods_id=gid, name=name, kind=v.kind, price_usd=v.cost,
                float_value=x.float_value, paint_seed=x.paint_seed, expected=v.expected,
                profit=v.profit, profit_pct=v.profit_pct, basis=v.basis, text=text,
                created_at=now_iso(now)))
            if sid and tg.send(text):
                self.store.mark_sent(sid)
                sent += 1
        self.store.mark_seen(gid, ids, now)

        self.store.polled(gid, now, 0, rate)
        if gap:
            self.store.set_status("last_gap", f"{name}: все {len(ids)} лотов страницы новые, "
                                              "возможен пропуск между обходами")
        log.info("%s: лотов %d, новых %d, сигналов %d, проверен в обходе по кругу",
                 name, len(ids), len(new), sent)
        return 0.0

    def session_expired(self, tg: Telegram, cookie: str) -> None:
        log.warning("BuffMarket: сессия истекла")
        self.store.set_status("session", "истекла")
        self.store.set_status("session_expired", cookie)
        self.alert_once(tg, "session_alerted", cookie,
                        "⚠️ BuffMarket: сессия истекла, опрос остановлен.\n"
                        "Обновите сессию на странице «Настройки» веб-панели.")

    def maybe_429(self, e: BuffError, tg: Telegram, now: datetime) -> None:
        if e.status != 429:
            return
        wait = e.retry_after if e.retry_after is not None else BACKOFF_429
        until = now_iso(now + timedelta(seconds=wait))
        self.store.set_status("pause_until", until)
        self.store.set_status("last_429", until)
        log.warning("BuffMarket: 429, пауза %ds", wait)
        self.alert_once(tg, "alerted_429", now.strftime("%Y-%m-%d"),
                        f"⚠️ BuffMarket ограничивает запросы (429), пауза до {until[11:16]} UTC.\n"
                        "Если повторяется — сократите список или увеличьте паузу.")

    def resolve(self, p: dict, s: dict, sec: dict, tg: Telegram, now: datetime,
                cookie: str) -> float:
        """Find one queued name's goods_id with the market's search. A Doppler
        phase is searched by its base name: BuffMarket sells all phases as one
        item and the listings carry the paint index."""
        name = p["name"]
        base = phases.split(name)[0]
        try:
            body = self.request("search", s, sec, now, query=base)
        except LoginRequired:
            self.session_expired(tg, cookie)
            return 0.0
        except BuffError as e:
            self.maybe_429(e, tg, self._request_finished_at or now)
            self.store.fail_pending(name, str(e))
            return 0.0
        except requests.RequestException as e:
            self.store.fail_pending(name, f"сеть: {type(e).__name__}")
            return 0.0
        self.store.set_status("session", "работает")
        gid = buff.match_goods(body, base)
        if gid is None:
            found = len((body.get("data") or {}).get("items") or [])
            self.store.fail_pending(name, f"не найден на BuffMarket (результатов поиска: {found})")
            log.info("%s: goods_id не найден", name)
        else:
            self.store.resolve_pending(name, gid)
            log.info("%s: goods_id %d, добавлен в опрос", name, gid)
        return 0.0

    def prune(self, now: datetime) -> None:
        if self._pruned is None or now - self._pruned > timedelta(hours=1):
            self.store.prune_seen(now)
            self.store.prune_requests(now)
            self._pruned = now


def daily_load(watch: list[dict], s: dict) -> tuple[float, float]:
    """Requests needed for a five-minute round, and the theoretical daily capacity."""
    need = sum(1 for w in watch if w["active"]) * 1440.0 / TARGET_CYCLE_MINUTES
    return need, 86400.0 / s["request_interval"]
