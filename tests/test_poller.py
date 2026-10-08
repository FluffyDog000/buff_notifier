"""The loop: one item, one request, one alert per listing, stops when told."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from notifier import config
from notifier.buff import BuffError, LoginRequired
from notifier.envfile import write_env
from notifier.poller import Poller, daily_load
from notifier.store import Store

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
NAME = "AK-47 | Redline (Field-Tested)"


def lot(i, price, f):
    return {"id": f"L{i}", "goods_id": 1, "price": str(price), "state": 1, "created_at": 1791377150,
            "asset_info": {"paintwear": str(f), "info": {"paintseed": i, "paintindex": 282}}}


def page(*lots):
    return {"code": "OK", "data": {"items": list(lots), "total_count": len(lots)}}


class Client:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def sell_orders(self, goods_id, page_size=10):
        self.calls.append(goods_id)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class TG:
    sent: list = []

    def __init__(self, token, chat):
        self.token = token

    def configured(self):
        return True

    def send(self, text):
        TG.sent.append(text)
        return True


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "csfloat.db"
    w = sqlite3.connect(db)
    w.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, market_hash_name TEXT, active INTEGER);
        CREATE TABLE sales (item_id INTEGER, price REAL, float_value REAL, paint_seed INTEGER,
                            paint_index INTEGER, sold_at TEXT);""")
    w.execute("INSERT INTO items VALUES (1, ?, 1)", (NAME,))
    w.executemany("INSERT INTO sales VALUES (1, ?, ?, 0, 282, ?)",
                  [(100.0, 0.165, (NOW - timedelta(days=i % 10 + 0.5)).isoformat()) for i in range(30)])
    w.commit()
    w.close()
    settings = tmp_path / "settings.json"
    config.save_settings({"csfloat_db": str(db)}, settings)
    envp = tmp_path / ".env"
    write_env(envp, {"BUFF_COOKIE": "session=1", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
    store = Store(tmp_path / "buff.db")
    store.add_watch(5777, NAME)
    TG.sent = []
    return store, settings, envp


def poller(env, client):
    store, settings, envp = env
    return Poller(store, settings, envp, client_factory=lambda s, sec: client,
                  telegram_factory=TG)


def test_a_cheap_listing_is_alerted_once(env):
    client = Client(page(lot(1, 80, 0.161), lot(2, 99, 0.162)), page(lot(1, 80, 0.161)))
    p = poller(env, client)
    p.cycle(NOW)
    assert len(TG.sent) == 1 and "💵 Buff: $80.00" in TG.sent[0] and "МСК" in TG.sent[0]
    w = env[0].watch_list()[0]
    assert w["sales_per_day"] == 1.0 and w["interval_min"] == 0, "low sales do not delay scanning"
    p.cycle(NOW + timedelta(seconds=5))
    assert client.calls == [5777, 5777] and len(TG.sent) == 1
    assert env[0].recent_signals()[0]["sent"] == 1


def test_vanilla_search_continues_to_exact_match_on_later_page(env):
    store, s_path, envp = env
    store.add_pending(['★ Survival Knife'])
    class SearchClient:
        calls = []
        def search_goods(self, query, page_size, page_num):
            self.calls.append((query, page_size, page_num))
            if page_num == 1:
                return {'data': {'total_page': 2, 'items': [{'id': 9, 'market_hash_name': '★ StatTrak™ Survival Knife'}]}}
            return {'data': {'total_page': 2, 'items': [{'id': 8540, 'market_hash_name': '★ Survival Knife'}]}}
    client = SearchClient()
    p = poller(env, client)
    s, sec = p.context()
    p.resolve(store.next_pending(), s, sec, TG('', ''), NOW, 'cookie')
    assert store.next_pending()['search_page'] == 2 and store.next_pending()['tries'] == 0
    restarted = poller(env, client)
    restarted.resolve(store.next_pending(), s, sec, TG('', ''), NOW + timedelta(seconds=5), 'cookie')
    assert not store.pending_list()
    assert any(w['goods_id'] == 8540 and w['name'] == '★ Survival Knife' for w in store.watch_list())
    assert client.calls == [('★ Survival Knife', 50, 1), ('★ Survival Knife', 50, 2)]


def test_all_items_are_scanned_without_waiting_for_old_timers_and_progress_survives_restart(env):
    store, _, _ = env
    store.add_watch(6000, NAME)
    store.polled(5777, NOW, 180, 0.01)
    store.polled(6000, NOW, 5, 1000)
    client = Client(page(), page(), page(), page())
    p = poller(env, client)
    p.cycle(NOW)
    p.cycle(NOW + timedelta(seconds=5))
    restarted = poller(env, client)
    restarted.cycle(NOW + timedelta(seconds=10))
    restarted.cycle(NOW + timedelta(seconds=15))
    assert client.calls == [5777, 6000, 5777, 6000]
    assert float(store.get_status("scan_round_seconds")) == 10


def test_failed_items_do_not_hold_up_others_and_inactive_items_are_skipped(env):
    store, _, _ = env
    store.add_watch(6000, NAME)
    store.add_watch(7000, NAME)
    store.set_active(6000, False)
    client = Client(BuffError("server error", 500), page(), page())
    p = poller(env, client)
    for seconds in (0, 5, 10):
        p.cycle(NOW + timedelta(seconds=seconds))
    assert client.calls == [5777, 7000, 5777]


def test_an_expired_session_stops_polling_until_a_new_one(env):
    store, _, envp = env
    client = Client(LoginRequired("x"), page())
    p = poller(env, client)
    p.cycle(NOW)
    assert "сессия истекла" in TG.sent[0]
    p.cycle(NOW + timedelta(minutes=1))
    p.cycle(NOW + timedelta(minutes=2))
    assert client.calls == [5777] and len(TG.sent) == 1, "one alert, no more requests"
    write_env(envp, {"BUFF_COOKIE": "session=2"})
    p.cycle(NOW + timedelta(minutes=3))
    assert client.calls == [5777, 5777] and store.get_status("session") == "работает"


def test_a_429_pauses_everything(env):
    client = Client(BuffError("429", 429, retry_after=600))
    p = poller(env, client)
    p.cycle(NOW)
    assert env[0].get_status("pause_until") == (NOW + timedelta(minutes=10)).isoformat()
    assert p.cycle(NOW + timedelta(minutes=5)) == 30.0 and client.calls == [5777]


def test_a_page_of_only_new_listings_reports_a_gap_while_scanning_continues(env):
    store, settings, _ = env
    config.save_settings({"page_size": 2}, settings)
    client = Client(page(lot(1, 99, 0.161), lot(2, 99, 0.162)),
                    page(lot(3, 99, 0.161), lot(4, 99, 0.162)))
    p = poller(env, client)
    p.cycle(NOW)
    p.cycle(NOW + timedelta(seconds=5))
    assert store.watch_list()[0]["interval_min"] == 0
    assert "пропуск" in store.get_status("last_gap")


def test_pause_and_a_missing_database_ask_nothing(env, tmp_path):
    _, settings, _ = env
    client = Client()
    config.save_settings({"paused": True}, settings)
    poller(env, client).cycle(NOW)
    config.save_settings({"paused": False, "csfloat_db": str(tmp_path / "nope.db")}, settings)
    poller(env, client).cycle(NOW)
    assert client.calls == [] and "база CSFloat" in env[0].get_status("state")


def test_load_is_for_a_five_minute_round_regardless_of_old_liquidity_timers():
    s = config.defaults()
    watch = [{"active": 1, "interval_min": 15.0}, {"active": 0, "interval_min": 5.0}]
    assert daily_load(watch, s) == (288.0, 17280.0)
