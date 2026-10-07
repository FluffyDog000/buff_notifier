"""Items picked from the bot's database, their goods_id found by the notifier."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from notifier import config
from notifier.buff import match_goods
from notifier.candidates import pick
from notifier.envfile import write_env
from notifier.poller import Poller
from notifier.store import Store

NOW = datetime.now(timezone.utc)


def bot_db(path):
    w = sqlite3.connect(path)
    w.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, market_hash_name TEXT, active INTEGER);
        CREATE TABLE sales (item_id INTEGER, price REAL, float_value REAL, paint_seed INTEGER,
                            paint_index INTEGER, sold_at TEXT);""")
    items = [(1, "AK-47 | Redline (Field-Tested)", 1), (2, "Cheap | Thing (Field-Tested)", 1),
             (3, "Slow | Thing (Field-Tested)", 1), (4, "★ Karambit | Fade (Factory New)", 1),
             (5, "★ Bayonet | Doppler Phase 2 (Factory New)", 1), (6, "Off | Thing (Field-Tested)", 0)]
    w.executemany("INSERT INTO items VALUES (?,?,?)", items)
    rows = []
    for i in range(300):                 # 10 a day, two floats with a 20% gap
        rows.append((1, 100.0 if i % 2 else 120.0, 0.161 if i % 2 else 0.151, i, 282,
                     (NOW - timedelta(days=i * 0.1)).isoformat()))
    rows += [(2, 1.0, 0.2, 0, 0, (NOW - timedelta(days=i * 0.1)).isoformat()) for i in range(300)]
    rows += [(3, 50.0, 0.2, 0, 0, (NOW - timedelta(days=i)).isoformat()) for i in range(10)]
    rows += [(4, 900.0, 0.01, 0, 0, (NOW - timedelta(days=i * 0.1)).isoformat()) for i in range(300)]
    rows += [(5, 500.0, 0.01, 0, 419, (NOW - timedelta(days=i * 0.2)).isoformat()) for i in range(150)]
    rows += [(6, 50.0, 0.2, 0, 0, (NOW - timedelta(days=i * 0.1)).isoformat()) for i in range(300)]
    rows += [(1, 1000.0, 0.15, 0, 0, (NOW - timedelta(days=40)).isoformat())]   # too old to count
    w.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?)", rows)
    w.commit()
    w.close()


def test_liquid_dear_enough_items_are_picked_by_liquidity(tmp_path):
    bot_db(tmp_path / "c.db")
    conn = sqlite3.connect(tmp_path / "c.db")
    got = pick(conn, config.defaults(), min_rate=1, min_price=5, limit=10, now=NOW)
    assert [c["name"] for c in got] == ["AK-47 | Redline (Field-Tested)",
                                        "★ Bayonet | Doppler Phase 2 (Factory New)"]
    ak = got[0]
    assert ak["rate"] == 10.0 and ak["median"] == 110.0 and round(ak["premium"], 2) == 0.20
    assert got[1]["premium"] is None, "one hundredth only"
    notify = dict(config.defaults(), pattern_skins="notify")
    names = [c["name"] for c in pick(conn, notify, 1, 5, 10, now=NOW)]
    assert "★ Karambit | Fade (Factory New)" in names
    assert pick(conn, config.defaults(), 1, 5, 10, exclude={ak["name"]}, now=NOW)[0] != ak


def test_the_search_answer_gives_the_exact_names_goods_id():
    body = {"code": "OK", "data": {"items": [
        {"id": 1, "market_hash_name": "AK-47 | Redline (Minimal Wear)"},
        {"id": 33960, "market_hash_name": "AK-47 | Redline (Field-Tested)"}]}}
    assert match_goods(body, "AK-47 | Redline (Field-Tested)") == 33960
    assert match_goods(body, "AK-47 | Redline (Well-Worn)") is None
    assert match_goods({"code": "OK", "data": {}}, "x") is None


def test_the_queue_skips_watched_names_and_gives_up_after_three_tries(tmp_path):
    store = Store(tmp_path / "b.db")
    store.add_watch(1, "Watched")
    assert store.add_pending(["Watched", "A", "A", "B", " "]) == 2
    assert store.next_pending()["name"] == "A"
    for _ in range(3):
        store.fail_pending("A", "не найден")
    assert store.next_pending()["name"] == "B"
    store.resolve_pending("B", 77)
    assert store.next_pending() is None
    assert {w["name"] for w in store.watch_list()} == {"Watched", "B"}
    store.remove_pending()
    assert store.pending_list() == []


class Client:
    def __init__(self):
        self.searched, self.polled = [], []

    def search_goods(self, q):
        self.searched.append(q)
        return {"code": "OK", "data": {"items": [{"id": 4242, "market_hash_name": q}]}}

    def sell_orders(self, gid, page_size=10):
        self.polled.append(gid)
        return {"code": "OK", "data": {"items": []}}


class TG:
    def __init__(self, *a):
        pass

    def configured(self):
        return False

    def send(self, text):
        return False


def test_the_notifier_finds_goods_ids_between_polls(tmp_path):
    bot_db(tmp_path / "c.db")
    config.save_settings({"csfloat_db": str(tmp_path / "c.db")}, tmp_path / "s.json")
    write_env(tmp_path / ".env", {"BUFF_COOKIE": "session=1"})
    store = Store(tmp_path / "b.db")
    store.add_pending(["★ Bayonet | Doppler Phase 2 (Factory New)"])
    client = Client()
    p = Poller(store, tmp_path / "s.json", tmp_path / ".env",
               client_factory=lambda s, sec: client, telegram_factory=TG)
    p.cycle(NOW)
    assert client.searched == ["★ Bayonet | Doppler (Factory New)"], "a phase is searched by its base"
    assert store.watch_list()[0]["goods_id"] == 4242
    assert store.watch_list()[0]["name"] == "★ Bayonet | Doppler Phase 2 (Factory New)"
    p.cycle(NOW)
    assert client.polled == [4242], "and then polled like any other"


def test_a_long_queue_still_drains_while_items_are_always_due(tmp_path):
    bot_db(tmp_path / "c.db")
    config.save_settings({"csfloat_db": str(tmp_path / "c.db")}, tmp_path / "s.json")
    write_env(tmp_path / ".env", {"BUFF_COOKIE": "session=1"})
    store = Store(tmp_path / "b.db")
    for gid in range(1, 20):
        store.add_watch(gid, "AK-47 | Redline (Field-Tested)" if gid == 1 else f"N{gid}")
    store.add_pending(["Slow | Thing (Field-Tested)"])
    client = Client()
    p = Poller(store, tmp_path / "s.json", tmp_path / ".env",
               client_factory=lambda s, sec: client, telegram_factory=TG)
    for _ in range(5):
        p.cycle(NOW)
    assert len(client.polled) == 4 and len(client.searched) == 1


def test_the_page_picks_and_queues(tmp_path):
    from werkzeug.security import generate_password_hash
    from web.app import create_app
    bot_db(tmp_path / "c.db")
    config.save_settings({"csfloat_db": str(tmp_path / "c.db")}, tmp_path / "s.json")
    write_env(tmp_path / ".env", {"WEB_PASSWORD_HASH": generate_password_hash("pw")})
    app = create_app(tmp_path / "b.db", tmp_path / "s.json", tmp_path / ".env")
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    c.post("/login", data={"password": "pw", "csrf": tok})
    page = c.get("/items?pick=1&min_rate=1&min_price=5&limit=10").get_data(as_text=True)
    assert "AK-47 | Redline (Field-Tested)" in page and "Добавить все 2" in page and "20%" in page
    names = re.search(r'<textarea name="names" hidden>(.*?)</textarea>', page, re.S).group(1)
    c.post("/items/queue", data={"csrf": tok, "names": names.replace("&#39;", "'")})
    c.post("/items/add", data={"csrf": tok, "lines": "Slow | Thing (Field-Tested)\nNot | Known"})
    store = Store(tmp_path / "b.db")
    assert [p["name"] for p in store.pending_list()] == [
        "AK-47 | Redline (Field-Tested)", "★ Bayonet | Doppler Phase 2 (Factory New)",
        "Slow | Thing (Field-Tested)"]
    page = c.get("/items").get_data(as_text=True)
    assert "Ищу goods_id (3)" in page and "нет среди предметов" in page
