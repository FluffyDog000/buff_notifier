"""The web page: locked by a password, every form guarded, secrets kept back."""
import re
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from notifier import config
from notifier.buff import LoginRequired
from notifier.envfile import read_env, write_env
from notifier.store import Store
from web.app import create_app

NAME = "M4A4 | Temukau (Field-Tested)"
CURL = ("curl 'https://api.buff.market/api/market/goods/sell_order?goods_id=24322' "
        "-b 'session=SECRETVALUE123; client_id=x' -H 'X-CSRFToken: tok' -H 'User-Agent: UA'")


class Client:
    calls: list = []
    answer = None

    def sell_orders(self, gid, page_size=10):
        Client.calls.append(gid)
        if isinstance(Client.answer, Exception):
            raise Client.answer
        return Client.answer or {"code": "OK", "data": {
            "goods_infos": {str(gid): {"market_hash_name": NAME}}, "items": []}}


class TG:
    sent: list = []

    def __init__(self, token, chat):
        self.token, self.chat = token, chat

    def configured(self):
        return bool(self.token and self.chat)

    def send(self, text):
        TG.sent.append(text)
        return True

    def chats(self):
        return [{"id": 42, "title": "Me"}]


@pytest.fixture
def app(tmp_path):
    db = tmp_path / "csfloat.db"
    w = sqlite3.connect(db)
    w.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, market_hash_name TEXT, active INTEGER);"
                    "CREATE TABLE sales (item_id INTEGER, price REAL, sold_at TEXT);")
    w.execute("INSERT INTO items VALUES (1, ?, 1)", (NAME,))
    w.commit()
    w.close()
    config.save_settings({"csfloat_db": str(db)}, tmp_path / "settings.json")
    write_env(tmp_path / ".env", {"WEB_PASSWORD_HASH": generate_password_hash("correct horse")})
    Client.calls, Client.answer, TG.sent = [], None, []
    a = create_app(tmp_path / "buff.db", tmp_path / "settings.json", tmp_path / ".env",
                   client_factory=lambda s, sec: Client(), telegram_factory=TG,
                   http_get=lambda *a, **k: type("R", (), {"json": lambda self: {"ip": "1.2.3.4"}})())
    a.testing = True
    a.tmp = tmp_path
    return a


def csrf(c, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).get_data(as_text=True)).group(1)


def logged_in(app):
    c = app.test_client()
    c.post("/login", data={"password": "correct horse", "csrf": csrf(c)})
    return c


def test_everything_but_login_needs_the_password(app):
    c = app.test_client()
    assert c.get("/").status_code == 302 and c.get("/settings").status_code == 302
    assert c.post("/login", data={"password": "wrong", "csrf": csrf(c)}).status_code == 401
    c.post("/login", data={"password": "correct horse", "csrf": csrf(c)})
    assert c.get("/").status_code == 200


def test_measurements_are_displayed_without_credentials(app):
    from datetime import datetime, timezone
    store = Store(app.tmp / "buff.db")
    now = datetime.now(timezone.utc)
    mid = store.measurement("opaque-digest", 5, True, now)
    store.record_request(mid, now, "poll", "limited", 429, None, 900)
    body = logged_in(app).get("/").get_data(as_text=True)
    assert "Замер запросов и ограничений" in body and "Первое через" in body
    assert "не указано" in body and "900 с" in body
    assert "opaque-digest" not in body


def test_five_wrong_passwords_lock_the_address(app):
    c = app.test_client()
    for _ in range(5):
        c.post("/login", data={"password": "nope", "csrf": csrf(c)})
    r = c.post("/login", data={"password": "correct horse", "csrf": csrf(c)})
    assert r.status_code == 429


def test_a_form_without_its_token_is_refused(app):
    c = logged_in(app)
    assert c.post("/settings", data={"min_discount": "20"}).status_code == 400


def test_without_a_password_hash_the_page_does_not_open(app):
    write_env(app.tmp / ".env", {"WEB_PASSWORD_HASH": None})
    r = app.test_client().get("/")
    assert r.status_code == 503 and "set_password" in r.get_data(as_text=True)


def test_the_secret_key_is_made_once_and_kept(app):
    assert len(read_env(app.tmp / ".env")["WEB_SECRET_KEY"]) == 64


def test_settings_are_saved_as_typed_and_checked(app):
    c = logged_in(app)
    form = {"csrf": csrf(c, "/settings"), "min_discount": "20", "min_profit_usd": "2",
            "float_signal": "on", "min_float_premium": "10", "normal_tolerance": "3",
            "pattern_skins": "skip", "csfloat_fee": "2", "buff_fee": "0", "usd_per_buff": "1",
            "window_days": "16", "request_interval": "6", "page_size": "10",
            "csfloat_db": str(app.tmp / "csfloat.db")}
    assert c.post("/settings", data=form).status_code == 302
    s = config.load_settings(app.tmp / "settings.json")
    assert s["min_discount"] == 0.2 and s["request_interval"] == 6.0
    assert s["float_signal"] is True and s["allow_item_median"] is False and s["paused"] is False
    assert s["cheap_signal"] is False, "missing checkbox switches off cheap alerts independently"
    assert 'name="cheap_signal"' in c.get('/settings').get_data(as_text=True)
    bad = dict(form, min_discount="abc")
    r = c.post("/settings", data=bad)
    assert r.status_code == 400 and "не число" in r.get_data(as_text=True)
    assert config.load_settings(app.tmp / "settings.json")["min_discount"] == 0.2


def test_overview_items_and_settings_explain_continuous_scanning(app):
    c = logged_in(app)
    assert "Обход по кругу" in c.get("/").get_data(as_text=True)
    assert "не влияют на очередь" in c.get("/items").get_data(as_text=True)
    body = c.get("/settings").get_data(as_text=True)
    assert 'name="poll_scale"' not in body and 'name="poll_max_minutes"' not in body
    assert "Опрос идёт непрерывно по кругу" in body


def test_items_explain_missing_comparable_sales_separately_from_network_errors(app):
    store = Store(app.tmp / 'buff.db')
    store.add_watch(24322, NAME)
    store.set_status('valuation:24322', 'Недостаточно похожих продаж: 2 из 10 лотов')
    body = logged_in(app).get('/items').get_data(as_text=True)
    assert 'Недостаточно похожих продаж: 2 из 10 лотов' in body
    assert store.watch_list()[0]['last_error'] is None
    store.conn.close()


def test_the_session_comes_from_curl_and_never_back_to_the_page(app):
    c = logged_in(app)
    r = c.post("/settings/session", data={"csrf": csrf(c, "/settings"), "curl": CURL},
               follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "session, client_id" in page and "SECRETVALUE123" not in page
    env = read_env(app.tmp / ".env")
    assert env["BUFF_COOKIE"] == "session=SECRETVALUE123; client_id=x" and env["BUFF_CSRF"] == "tok"
    assert "SECRETVALUE123" not in c.get("/settings").get_data(as_text=True)


def test_the_session_check_says_when_it_has_expired(app):
    c = logged_in(app)
    Client.answer = LoginRequired("x")
    r = c.post("/settings/session/test", data={"csrf": csrf(c, "/settings")}, follow_redirects=True)
    assert "нужен вход" in r.get_data(as_text=True)


def test_one_proxy_is_stored_and_shown_without_its_password(app):
    c = logged_in(app)
    c.post("/settings/proxy", data={"csrf": csrf(c, "/settings"), "proxy": "http://u:pw123@1.2.3.4:8000"})
    assert read_env(app.tmp / ".env")["BUFF_PROXY"] == "http://u:pw123@1.2.3.4:8000"
    page = c.get("/settings").get_data(as_text=True)
    assert "http://1.2.3.4:8000" in page and "pw123" not in page
    r = c.post("/settings/proxy/test", data={"csrf": csrf(c, "/settings")}, follow_redirects=True)
    assert "1.2.3.4" in r.get_data(as_text=True)
    c.post("/settings/proxy", data={"csrf": csrf(c, "/settings"), "clear": "1"})
    assert "BUFF_PROXY" not in read_env(app.tmp / ".env")


def test_telegram_token_is_kept_when_left_blank(app):
    c = logged_in(app)
    c.post("/settings/telegram", data={"csrf": csrf(c, "/settings"), "token": "123:abcdefghijk", "chat_id": "1"})
    c.post("/settings/telegram", data={"csrf": csrf(c, "/settings"), "token": "", "chat_id": "42"})
    env = read_env(app.tmp / ".env")
    assert env["TELEGRAM_BOT_TOKEN"] == "123:abcdefghijk" and env["TELEGRAM_CHAT_ID"] == "42"
    assert "123:abcdefghijk" not in c.get("/settings").get_data(as_text=True)
    c.post("/settings/telegram/test", data={"csrf": csrf(c, "/settings")})
    assert TG.sent and "Проверка" in TG.sent[0]


def test_items_are_added_by_goods_id_and_checked_against_csfloat(app):
    c = logged_in(app)
    token = csrf(c, "/items")
    c.post("/items/add", data={"csrf": token, "lines": "24322\n555;AK-47 | Nope (Field-Tested)\nxyz"})
    store = Store(app.tmp / "buff.db")
    assert [(w["goods_id"], w["name"]) for w in store.watch_list()] == [(24322, NAME)]
    assert Client.calls == [24322], "the name was asked of BuffMarket only where missing"
    page = c.get("/items").get_data(as_text=True)
    assert NAME in page and "нет среди предметов" in page and "goods_id" in page
    c.post("/items/24322/toggle", data={"csrf": token})
    assert store.watch_list()[0]["active"] == 0
    c.post("/items/24322/delete", data={"csrf": token})
    assert store.watch_list() == []


def test_the_overview_shows_signals_and_state(app):
    store = Store(app.tmp / "buff.db")
    store.add_signal(dict(listing_id="L1", goods_id=1, name=NAME, kind="cheap", price_usd=80.0,
                          float_value=0.16, paint_seed=1, expected=100.0, profit=18.0,
                          profit_pct=0.225, basis="b", text="TEXT", created_at="2026-10-07T12:00:00+00:00"))
    store.set_status("session", "истекла")
    page = logged_in(app).get("/").get_data(as_text=True)
    assert NAME in page and "$18.00" in page and "истекла" in page and "1 предметов" in page


def test_items_selection_is_explicit_guarded_and_preserves_filters(app):
    c = logged_in(app)
    st = Store(app.tmp / 'buff.db')
    for gid in (1, 2, 3):
        st.add_watch(gid, f'Item {gid}')
    assert c.post('/items/selection', data={'ids': ['1', '2'], 'action': 'delete'}).status_code == 400
    token = csrf(c, '/items')
    r = c.post('/items/selection', data={'csrf': token, 'ids': ['1', '2'], 'action': 'disable', 'q': 'Item'})
    assert 'q=Item' in r.location
    assert [w['active'] for w in st.watch_list()] == [0, 0, 1]
    assert c.post('/items/selection', data={'csrf': token, 'ids': ['bad'], 'action': 'delete'}).status_code == 400
    assert len(st.watch_list()) == 3
    c.post('/items/selection', data={'csrf': token, 'ids': ['1', '2'], 'action': 'delete'})
    assert [w['goods_id'] for w in st.watch_list()] == [3]


def test_lists_filter_and_paginate_and_render_compact_accounts(app):
    c = logged_in(app)
    st = Store(app.tmp / 'buff.db')
    for gid in range(65):
        st.add_watch(gid, f'Skin {gid:03}')
    body = c.get('/items?per_page=25&page=2').get_data(as_text=True)
    assert 'Skin 025' in body and 'Skin 049' in body and 'Skin 050' not in body and 'Skin 000' not in body
    body = c.get('/items?q=Skin+064').get_data(as_text=True)
    assert 'Skin 064' in body and 'Skin 063' not in body
    body = c.get('/accounts').get_data(as_text=True)
    assert 'accounts-selection' in body and 'name="q"' in body and '<summary>Управление</summary>' in body


def test_vanilla_bare_name_resolves_to_csfloat_name_and_pending_can_retry(app):
    db = sqlite3.connect(app.tmp / 'csfloat.db')
    db.execute('INSERT INTO items VALUES (2, ?, 1)', ('★ Survival Knife',))
    db.commit()
    db.close()
    c = logged_in(app)
    token = csrf(c, '/items')
    c.post('/items/add', data={'csrf': token, 'lines': 'Survival Knife'})
    st = Store(app.tmp / 'buff.db')
    assert st.pending_list()[0]['name'] == '★ Survival Knife'
    st.search_progress('★ Survival Knife', 3, True)
    assert not st.next_pending()
    c.post('/items/pending/retry', data={'csrf': token})
    assert st.next_pending()['tries'] == 0 and st.next_pending()['search_page'] == 1


def test_account_bulk_enable_checks_all_sessions_before_any_change(app):
    from notifier.accounts import Accounts, profile_key
    reg = Accounts(app.tmp / 'accounts.json', app.tmp / '.env')
    a = reg.save(None, 'Ready', {'BUFF_COOKIE': 'session=1'}, 'http://proxy:80', 5)
    b = reg.save(None, 'Not ready', {'BUFF_COOKIE': 'session=2'}, 'http://proxy:80', 5)
    reg.tested(a, profile_key(next(p for p in reg.list() if p['id'] == a)), '1.2.3.4')
    c = logged_in(app)
    token = csrf(c, '/accounts')
    c.post('/accounts/selection', data={'csrf': token, 'ids': [a, b], 'action': 'enable'})
    assert not any(p['enabled'] for p in reg.list() if p['id'] in (a, b))
    c.post('/accounts/selection', data={'csrf': token, 'ids': [a], 'action': 'enable'})
    assert next(p for p in reg.list() if p['id'] == a)['enabled']
    c.post('/accounts/selection', data={'csrf': token, 'ids': [a, b], 'action': 'disable'})
    assert not any(p['enabled'] for p in reg.list() if p['id'] in (a, b))


def test_accounts_require_login_and_csrf_and_never_render_credentials(app):
    from notifier.accounts import Accounts
    assert app.test_client().get("/accounts").status_code == 302
    c = logged_in(app)
    assert c.post("/accounts/save", data={"label": "Two", "curl": CURL}).status_code == 400
    token = csrf(c, "/accounts")
    c.post("/accounts/save", data={"csrf": token, "label": "Two", "curl": CURL,
                                  "proxy": "http://privateuser:privatepass@proxy.example:80", "interval": "5"})
    registry = Accounts(app.tmp / "accounts.json", app.tmp / ".env")
    a = registry.list()[1]
    assert not a["enabled"]
    c.post(f"/accounts/{a['id']}/toggle", data={"csrf": token})
    assert not registry.list()[1]["enabled"]
    r = c.post(f"/accounts/{a['id']}/test", data={"csrf": token}, follow_redirects=True)
    assert "Сессия работает" in r.get_data(as_text=True)
    c.post(f"/accounts/{a['id']}/toggle", data={"csrf": token})
    assert registry.list()[1]["enabled"]
    body = c.get("/accounts").get_data(as_text=True)
    assert "SECRETVALUE123" not in body and "privatepass" not in body and "privateuser" not in body
    assert "http://proxy.example:80" in body and "1.2.3.4" in body
    c.post(f"/accounts/{a['id']}/remove", data={"csrf": token})
    assert len(registry.list()) == 1


def test_account_test_respects_existing_direct_ip_cooldown(app):
    from datetime import datetime, timedelta, timezone
    from notifier.accounts import Accounts
    write_env(app.tmp / ".env", {"BUFF_COOKIE": "primary"})
    registry = Accounts(app.tmp / "accounts.json", app.tmp / ".env")
    aid = registry.save(None, "Two", {"BUFF_COOKIE": "other"}, "http://proxy.example:80", 5)
    store = Store(app.tmp / "buff.db")
    store.pause_route("direct", (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
    c = logged_in(app)
    body = c.post(f"/accounts/{aid}/test", data={"csrf": csrf(c, "/accounts")}, follow_redirects=True).get_data(as_text=True)
    assert "IP занят или выдерживает паузу" in body and Client.calls == []
    assert "egress_ip" not in registry.list()[1]
