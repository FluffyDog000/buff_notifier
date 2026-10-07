"""The polling loop: one due item at a time, one request at a time.

Each cycle re-reads settings and `.env`, so whatever the web page changes
applies on the next item. The schedule follows the item's liquidity on
CSFloat, the same idea as the bot's collector:

    interval = poll_scale / sales per day, within [poll_min, poll_max] minutes

and a page made only of listings we have not seen means some may have
slipped past between polls: the next one comes at the minimum.

Stops asking BuffMarket, and says so in Telegram once:
- the session has expired (`Login Required`) - until a new one is saved;
- 429 - for as long as the site asks, 15 minutes when it does not say.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import buff, config, sales
from .buff import BuffError, LoginRequired
from .judge import HISTORY_DAYS, Market, judge, message
from .listings import item_url, parse_page
from .store import Store, now_iso
from .telegram import Telegram

log = logging.getLogger("buff.poller")

RATE_DAYS = 30.0
BACKOFF_429 = 15 * 60
IDLE = 30.0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def interval_minutes(sales_per_day: float, s: dict) -> float:
    raw = s["poll_scale"] / max(sales_per_day, 0.01)
    return min(max(raw, s["poll_min_minutes"]), s["poll_max_minutes"])


class Poller:
    def __init__(self, store: Store, settings_path: Path = config.SETTINGS_PATH,
                 env_path: Path = config.ENV_PATH, client_factory=buff.from_config,
                 telegram_factory=Telegram, connect=sales.connect):
        self.store = store
        self.settings_path, self.env_path = settings_path, env_path
        self.client_factory, self.telegram_factory, self.connect = \
            client_factory, telegram_factory, connect
        self._client, self._client_key = None, None
        self._db, self._db_path = None, None
        self._pruned: datetime | None = None

    # -- helpers --------------------------------------------------------------

    def client(self, s: dict, sec: dict):
        key = buff.fingerprint(s, sec)
        if self._client is None or key != self._client_key:
            self._client, self._client_key = self.client_factory(s, sec), key
        return self._client

    def csfloat(self, path: str) -> sqlite3.Connection:
        if self._db is None or path != self._db_path:
            if not Path(path).exists():
                raise FileNotFoundError(path)
            self._db, self._db_path = self.connect(path), path
        return self._db

    def alert_once(self, tg: Telegram, key: str, marker: str, text: str) -> None:
        """Send `text` unless this `marker` was already alerted under `key`."""
        if self.store.get_status(key) == marker:
            return
        if tg.send(text) or not tg.configured():
            self.store.set_status(key, marker)

    # -- one cycle ------------------------------------------------------------

    def cycle(self, now: datetime | None = None) -> float:
        """Poll at most one item. Returns seconds to wait before the next cycle."""
        now = now or datetime.now(timezone.utc)
        s = config.load_settings(self.settings_path)
        sec = config.load_secrets(self.env_path)
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

        item = self.store.next_due(now)
        if item is None:
            self.store.set_status("state", "работает")
            soon = self.store.soonest()
            return IDLE if soon is None else max(1.0, min(IDLE, (soon - now).total_seconds()))

        self.prune(now)
        return self.poll(item, s, sec, db, tg, now, cookie)

    def poll(self, item: dict, s: dict, sec: dict, db, tg: Telegram, now: datetime,
             cookie: str) -> float:
        gid, name = item["goods_id"], item["name"]
        try:
            body = self.client(s, sec).sell_orders(gid, page_size=s["page_size"])
        except LoginRequired:
            log.warning("BuffMarket: сессия истекла")
            self.store.set_status("session", "истекла")
            self.store.set_status("session_expired", cookie)
            self.alert_once(tg, "session_alerted", cookie,
                            "⚠️ BuffMarket: сессия истекла, опрос остановлен.\n"
                            "Обновите сессию на странице «Настройки» веб-панели.")
            return 0.0
        except BuffError as e:
            if e.status == 429:
                wait = e.retry_after or BACKOFF_429
                until = now_iso(now + timedelta(seconds=wait))
                self.store.set_status("pause_until", until)
                self.store.set_status("last_429", until)
                log.warning("BuffMarket: 429, пауза %ds", wait)
                self.alert_once(tg, "alerted_429", now.strftime("%Y-%m-%d"),
                                f"⚠️ BuffMarket ограничивает запросы (429), пауза до {until[11:16]} UTC.\n"
                                "Если повторяется — сократите список или увеличьте паузу.")
            self.store.polled(gid, now, s["poll_min_minutes"], None, str(e))
            return 0.0
        except requests.RequestException as e:
            self.store.polled(gid, now, s["poll_min_minutes"], None, f"сеть: {e}")
            return 0.0

        self.store.set_status("session", "работает")
        page = parse_page(body)
        ids = [x.id for x in page.listings]
        new = self.store.unseen(ids)
        gap = item["polls"] > 0 and len(ids) >= s["page_size"] and new == set(ids)

        item_id = sales.item_id(db, name)
        if item_id is None:
            self.store.mark_seen(gid, ids, now)
            self.store.polled(gid, now, s["poll_max_minutes"], None,
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

        minutes = s["poll_min_minutes"] if gap else interval_minutes(rate, s)
        self.store.polled(gid, now, minutes, rate)
        if gap:
            self.store.set_status("last_gap", f"{name}: все {len(ids)} лотов страницы новые, "
                                              "возможен пропуск — опрос чаще")
        log.info("%s: лотов %d, новых %d, сигналов %d, следующий опрос через %.0f мин",
                 name, len(ids), len(new), sent, minutes)
        return 0.0

    def prune(self, now: datetime) -> None:
        if self._pruned is None or now - self._pruned > timedelta(hours=1):
            self.store.prune_seen(now)
            self._pruned = now


def daily_load(watch: list[dict], s: dict) -> tuple[float, float]:
    """(requests a day the active list needs, requests a day the pause allows)."""
    need = sum(1440.0 / (w["interval_min"] or s["poll_min_minutes"])
               for w in watch if w["active"])
    return need, 86400.0 / s["request_interval"]
