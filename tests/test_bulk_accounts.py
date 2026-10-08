"""Bulk import is atomic, repeatable, private, and uses one browser at a time."""
import hashlib
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from notifier.accounts import Accounts, login_key, profile_key
from notifier.bulk_accounts import parse_bulk, proxy_line
from notifier.browser_login import BrowserLogins
from notifier.store import Store
from tests.test_browser_login import manager, Page
from tests.test_poller import env  # noqa: F401
from tests.test_web import app, logged_in, csrf  # noqa: F401
from web.app import create_app


def finish(batch):
    batch.thread.join(timeout=6)
    assert batch.done, batch.public()


def fake_success(mgr, job, profile):
    class Context:
        def cookies(self, urls):
            return [{"name": "session", "value": job.aid}]
    store = Store(mgr.store_path)
    try:
        mgr._save(job, profile, store, Context(), Page(), {}, "1.2.3.4", "ip:1.2.3.4")
    finally:
        store.conn.close()


def test_formats_preserve_passwords_and_proxy_credentials():
    rows = parse_bulk('alice:pass:with:colons\nbob;"pass;with""quote";host:80:user:p@ss:word')
    assert rows[0]["password"] == "pass:with:colons" and rows[0]["proxy"] == ""
    assert rows[1]["password"] == 'pass;with"quote'
    assert rows[1]["proxy"] == "http://user:p%40ss%3Aword@host:80"
    assert proxy_line("https://u:p@host:443") == "https://u:p@host:443"
    assert proxy_line("socks5://[::1]:1080") == "socks5://[::1]:1080"
    rows = parse_bulk("\nalice:p\n\nbob:p", "host:80\n\n-")
    assert [r["line"] for r in rows] == [2, 4]
    assert [r["proxy"] for r in rows] == ["http://host:80", ""]
    assert len({r["proxy"] for r in parse_bulk("alice:p\nbob:p", "host:80", True)}) == 1


@pytest.mark.parametrize("accounts,proxies", [
    ("", ""), ("alice", ""), ("alice:", ""), ("alice:secret\nALICE:secret", ""),
    ("alice;secret;bad-private-proxy", ""), ("alice:p\nbob:p", "host:80"),
    ("alice;p;host:80", "host:80"), ('alice;"secret', ""),
    ("alice:p", "socks5://private-user:secret@host:1080"),
])
def test_invalid_lists_fail_without_echoing_secrets(accounts, proxies):
    with pytest.raises(ValueError) as failure:
        parse_bulk(accounts, proxies)
    assert "secret" not in str(failure.value) and "private" not in str(failure.value)


def test_import_atomic_reimport_dedupes_and_password_never_written(env):
    registry = Accounts(env[2].parent / "accounts.json", env[2])
    first = registry.import_bulk(parse_bulk("alice:PRIVATEPASSWORD\nbob:OTHERPASSWORD", "host:80\n-"), 5)
    text = registry.path.read_text(encoding="utf-8")
    assert "PRIVATEPASSWORD" not in text and "OTHERPASSWORD" not in text
    assert all(not a["enabled"] and not a["BUFF_COOKIE"] for a in registry.list()[1:])
    again = registry.import_bulk(parse_bulk("ALICE:newpass\nbob:newpass", "host:80\n-"), 2)
    assert [r["id"] for r in again] == [r["id"] for r in first]
    assert not any(r["created"] for r in again) and len(registry.list()) == 3
    assert registry.list()[1]["interval"] == 5
    unchanged = registry.path.read_bytes()
    with pytest.raises(ValueError, match="другой прокси"):
        registry.import_bulk(parse_bulk("charlie:p\nalice:p", "-\nother:80"), 5)
    assert registry.path.read_bytes() == unchanged
    with pytest.raises(ValueError):
        registry.import_bulk(parse_bulk("charlie:p"), float("nan"))
    assert registry.path.read_bytes() == unchanged


def test_capacity_validation_never_partially_adds(env):
    registry = Accounts(env[2].parent / "accounts.json", env[2])
    registry.import_bulk(parse_bulk("\n".join(f"u{i}:p" for i in range(198))), 5)
    unchanged = registry.path.read_bytes()
    with pytest.raises(ValueError, match="свободных"):
        registry.import_bulk(parse_bulk("last1:p\nlast2:p"), 5)
    assert registry.path.read_bytes() == unchanged and len(registry.list()) == 199


def test_old_browser_profile_reused_and_session_identity_deduped(env):
    registry = Accounts(env[2].parent / "accounts.json", env[2])
    marker = registry.path.parent / "browser_profiles" / "primary" / hashlib.sha256(b"").hexdigest()[:16] / "last_profile"
    marker.parent.mkdir(parents=True)
    marker.write_text(login_key("alice")[:32], encoding="ascii")
    row = registry.import_bulk(parse_bulk("alice:pass"), 5)[0]
    assert row["id"] == "primary" and not row["created"] and len(registry.list()) == 1
    assert registry.list()[0]["steam_login_hash"] == login_key("alice")
    aid = registry.save(None, "Other", interval=5)
    profile = registry.list()[1]
    with pytest.raises(ValueError, match="Steam-логин"):
        registry.receive_session(aid, profile_key(profile), {"BUFF_COOKIE": "new"}, "1.2.3.4", login_key("alice"))
    assert not registry.list()[1]["BUFF_COOKIE"]


def test_sequential_queue_error_continues_auto_enable_and_reimport_skips_healthy(env):
    calls, running, maximum = [], 0, 0
    class Client:
        def sell_orders(self, *args, **kw):
            return {"code": "OK"}
    mgr = manager(env, factory=lambda s, sec: Client())
    def runner(job, profile):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        calls.append(job.credentials[0])
        try:
            if job.credentials[0] == "bad":
                job.update(state="Ошибка входа")
            else:
                fake_success(mgr, job, profile)
        finally:
            running -= 1
            job.credentials = None
            job.update(done=True)
    mgr.runner = runner
    try:
        rows = parse_bulk("alice:PRIVATEPASSWORD\nbad:p\nbob:p")
        batch = mgr.start_bulk(rows, "owner", 5)
        finish(batch)
        assert maximum == 1 and calls == ["alice", "bad", "bob"]
        assert [r["state"] for r in batch.public()["rows"]] == ["ok", "error", "ok"]
        assert [a["enabled"] for a in mgr.accounts.list()[1:]] == [True, False, True]
        assert not any(r["password"] for r in batch.entries)
        assert "PRIVATEPASSWORD" not in json.dumps(batch.public())
        resumed = mgr.start_bulk(rows, "owner", 5)
        finish(resumed)
        assert calls == ["alice", "bad", "bob", "bad"]
        assert resumed.public()["created"] == 0
        assert [r["state"] for r in resumed.public()["rows"]] == ["ready", "error", "ready"]
    finally:
        mgr.close()


def test_auto_enable_off_and_changed_verified_session_never_enabled(env):
    class Client:
        def sell_orders(self, *args, **kw):
            return {"code": "OK"}
    mgr = manager(env, factory=lambda s, sec: Client())
    def runner(job, profile):
        try:
            fake_success(mgr, job, profile)
            if job.credentials[0] == "changed":
                mgr.accounts.save(job.aid, profile["label"], {"BUFF_COOKIE": "edited"}, interval=5)
        finally:
            job.credentials = None
            job.update(done=True)
    mgr.runner = runner
    try:
        batch = mgr.start_bulk(parse_bulk("alice:p"), "owner", 5, auto_enable=False)
        finish(batch)
        assert batch.public()["ok"] == 1 and not mgr.accounts.list()[1]["enabled"]
        changed = mgr.start_bulk(parse_bulk("changed:p"), "owner", 5)
        finish(changed)
        assert changed.public()["failed"] == 1 and not mgr.accounts.list()[2]["enabled"]
    finally:
        mgr.close()


def test_cancel_clears_pending_passwords_busy_import_is_atomic_and_closed_manager_refuses(env):
    started = threading.Event()
    def runner(job, a):
        started.set()
        job.stop.wait(5)
        job.credentials = None
        job.update(done=True)
    mgr = manager(env, runner=runner)
    batch = mgr.start_bulk(parse_bulk("alice:private\nbob:private"), "owner", 5)
    try:
        assert started.wait(2)
        unchanged = mgr.accounts.path.read_bytes()
        with pytest.raises(ValueError, match="завершите"):
            mgr.start_bulk(parse_bulk("charlie:p"), "owner", 5)
        assert unchanged == mgr.accounts.path.read_bytes()
        assert mgr.get_batch(batch.id, "other") is None
        mgr.cancel_batch(batch)
        finish(batch)
        assert not any(entry["password"] for entry in batch.entries)
        assert not any(a["enabled"] for a in mgr.accounts.list()[1:])
        assert batch.public()["rows"][1]["state"] == "cancelled"
    finally:
        mgr.close()
    with pytest.raises(ValueError, match="перезапускается"):
        mgr.start_bulk(parse_bulk("charlie:p"), "owner", 5)


def test_bulk_web_owner_csrf_private_status_cancel_and_validation(app):
    started = threading.Event()
    def runner(job, a):
        started.set()
        job.stop.wait(5)
        job.credentials = None
        job.update(done=True)
    def factory(*args, **kw):
        return BrowserLogins(*args, **kw, browser_runner=runner)
    web = create_app(app.tmp / "buff.db", app.tmp / "settings.json", app.tmp / ".env", login_factory=factory)
    c = logged_in(web)
    token = csrf(c, "/accounts")
    mgr = web.extensions["browser_logins"]
    try:
        assert c.post("/accounts/bulk", data={"accounts": "alice:p"}).status_code == 400
        response = c.post("/accounts/bulk", data={"csrf": token, "accounts": "alice:p\nbob:p", "proxies": "host:80"})
        assert response.headers["Location"] == "/accounts" and len(mgr.accounts.list()) == 1
        response = c.post("/accounts/bulk", data={"csrf": token, "accounts": "alice:PRIVATEPASSWORD\nbob:PRIVATEPASSWORD",
                                                "proxies": "host:80:user:PROXYSECRET\n-", "interval": "5", "auto_enable": "on"})
        path = response.headers["Location"]
        assert path.startswith("/accounts/bulk/") and started.wait(2)
        body = c.get(path).get_data(as_text=True)
        status = c.get(path + "/status")
        assert status.status_code == 200 and status.headers["Cache-Control"] == "no-store"
        assert "PRIVATEPASSWORD" not in body + status.get_data(as_text=True)
        assert "PROXYSECRET" not in body + status.get_data(as_text=True)
        assert path in c.get("/accounts").get_data(as_text=True)
        other = logged_in(web)
        assert other.get(path).status_code == 404 and other.get(path+"/status").status_code == 404
        assert web.test_client().get(path).status_code == 302
        assert other.post(path+"/cancel", data={"csrf": csrf(other, "/accounts")}).status_code == 404
        assert c.post(path+"/cancel").status_code == 400
        assert c.post(path+"/cancel", data={"csrf": token}).status_code == 302
        finish(next(iter(mgr.batches.values())))
    finally:
        mgr.close()


def test_logout_cancels_queue_and_clears_remaining_credentials(app):
    def runner(job, a):
        job.stop.wait(5)
        job.credentials = None
        job.update(done=True)
    def factory(*args, **kw):
        return BrowserLogins(*args, **kw, browser_runner=runner)
    web = create_app(app.tmp / "buff.db", app.tmp / "settings.json", app.tmp / ".env", login_factory=factory)
    c = logged_in(web)
    token = csrf(c, "/accounts")
    mgr = web.extensions["browser_logins"]
    try:
        c.post("/accounts/bulk", data={"csrf": token, "accounts": "alice:p\nbob:p"})
        batch = next(iter(mgr.batches.values()))
        assert c.post("/logout", data={"csrf": token}).status_code == 302
        finish(batch)
        assert not any(r["password"] for r in batch.entries)
    finally:
        mgr.close()


@pytest.mark.skipif(os.environ.get("BUFF_TEST_BROWSER") != "1", reason="Requires installed Chromium")
def test_real_browser_bulk_form_progress_and_cancel(app):
    """Exercise the actual HTML/JS; intercept every request with a private test app."""
    from playwright.sync_api import sync_playwright
    started = threading.Event()
    def runner(job, a):
        started.set()
        job.update(state="Требуется капча")
        job.stop.wait(15)
        job.credentials = None
        job.update(done=True)
    def factory(*args, **kw):
        return BrowserLogins(*args, **kw, browser_runner=runner)
    web = create_app(app.tmp / "buff.db", app.tmp / "settings.json", app.tmp / ".env", login_factory=factory)
    c = logged_in(web)
    mgr = web.extensions["browser_logins"]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 760})
            def serve(route):
                request = route.request
                u = urlsplit(request.url)
                if u.hostname != "panel.test":
                    route.abort()
                    return
                fields = {k: v[0] for k, v in parse_qs(request.post_data or "", keep_blank_values=True).items()}
                response = c.open(u.path + ("?" + u.query if u.query else ""), method=request.method,
                                  data=fields, follow_redirects=True)
                route.fulfill(status=response.status_code, body=response.data,
                              content_type=response.content_type)
            page.route("**/*", serve)
            page.goto("https://panel.test/accounts")
            page.get_by_text("Массовое добавление аккаунтов и прокси", exact=True).click()
            page.locator('[data-bulk-file="accounts"]').set_input_files(dict(name="accounts.txt", mimeType="text/plain", buffer="\ufeffalice:FAKEPASSWORD\nbob:FAKEPASSWORD".encode()))
            page.wait_for_function("document.querySelector('textarea[name=accounts]').value.startsWith('alice:')")
            page.locator('[data-bulk-file="proxies"]').set_input_files(dict(name="proxies.txt", mimeType="text/plain", buffer=b"host:80\n-"))
            page.wait_for_function("document.querySelector('textarea[name=proxies]').value === 'host:80\\n-'")
            assert 'Загружено строк: 2' in page.locator('[data-bulk-file-status]').inner_text()
            page.get_by_role("button", name="Добавить и запустить массовый вход", exact=True).click()
            page.wait_for_function("document.querySelector('#bulk-summary') && document.querySelector('.bulk-window:not([hidden])')")
            assert started.wait(2)
            assert page.locator('.bulk-window:not([hidden])').count() == 1
            assert page.locator('.bulk-message').first.inner_text() == "Требуется капча"
            assert "FAKEPASSWORD" not in page.content()
            page.get_by_role("button", name="Остановить очередь", exact=True).click()
            page.wait_for_function("document.querySelector('#bulk-summary').textContent.includes('Очередь завершена')")
            assert page.locator('#bulk-cancel').is_hidden()
            assert not page.locator('.bulk-window:not([hidden])').count()
            browser.close()
    finally:
        mgr.close()
