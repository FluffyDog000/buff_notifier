"""Credentials stay private; sessions require validation and ownership guards."""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from notifier.accounts import Accounts, profile_key
from notifier.browser_login import BrowserLogins, Job, allowed_page, browser_proxy
from notifier.buff import BuffError, LoginRequired
from notifier.envfile import read_env
from notifier.store import Store
from tests.test_poller import env, NOW  # noqa: F401
from tests.test_web import app, logged_in, csrf  # noqa: F401
from web.app import create_app


def manager(env, factory=lambda s, sec: None, runner=None):
    store, settings, envp = env
    registry = Accounts(envp.parent / "accounts.json", envp)
    ip = lambda *args, **kw: type("R", (), {"json": lambda self: {"ip": "1.2.3.4"}})()
    return BrowserLogins(registry, envp.parent / "buff.db", settings, factory, ip, runner)


def test_proxy_credentials_and_navigation_domains():
    assert browser_proxy("http://u:p%40ss@host:8000") == dict(server="http://host:8000", username="u", password="p@ss")
    assert browser_proxy("socks5h://host:1080") == dict(server="socks5://host:1080")
    with pytest.raises(ValueError, match="HTTP/HTTPS"):
        browser_proxy("socks5://u:p@host:1080")
    assert allowed_page("https://steamcommunity.com/openid/login")
    assert allowed_page("https://api.buff.market/account/login/steam")
    assert not allowed_page("https://steamcommunity.com.evil.example/")
    assert not allowed_page("http://steamcommunity.com/login")
    assert not allowed_page("http://127.0.0.1:5050/")


def test_jobs_are_owned_bounded_and_cancelled(env):
    def runner(job, a):
        job.stop.wait(3)
        job.credentials = None
        job.update(done=True)
    mgr = manager(env, runner=runner)
    job = mgr.start("primary", "owner", "alice", "privatepass")
    try:
        assert mgr.get(job.id, "wrong") is None and mgr.get(job.id, "owner") is job
        assert "privatepass" not in str(job.public()) and "alice" not in str(job.public())
        with pytest.raises(ValueError, match="завершите"):
            mgr.start("primary", "other")
        with pytest.raises(ValueError):
            job.action("click", {"x": "nan", "y": "0"})
        with pytest.raises(ValueError):
            job.action("key", {"key": "F12"})
        for _ in range(32):
            job.action("key", {"key": "Tab"})
        with pytest.raises(ValueError, match="Дождитесь"):
            job.action("text", {"text": "secret"})
    finally:
        mgr.close()
    assert job.done and job.credentials is None


class Context:
    def cookies(self, urls):
        return [{"name": "session", "value": "NEWSESSION"}, {"name": "csrf_token", "value": "CSRF"}]


class Page:
    def evaluate(self, expression):
        return "Browser UA"


@pytest.mark.parametrize("failure", [LoginRequired("expired"), BuffError("limit", 429, 300)])
def test_invalid_session_never_replaces_live_cookie_and_429_keeps_cooldown(env, failure):
    class Client:
        def sell_orders(self, *args, **kwargs):
            raise failure
    mgr = manager(env, lambda s, sec: Client())
    a = mgr.accounts.list()[0]
    job = Job("primary", "Main", "owner", None)
    if failure.status == 429:
        with pytest.raises(ValueError, match="429"):
            mgr._save(job, a, env[0], Context(), Page(), {}, "1.2.3.4", "ip:1.2.3.4")
        assert env[0].route_pause("ip:1.2.3.4")
    else:
        assert not mgr._save(job, a, env[0], Context(), Page(), {}, "1.2.3.4", "ip:1.2.3.4")
    assert read_env(env[2])["BUFF_COOKIE"] == "session=1"


def test_validated_session_saved_atomically_and_changed_profile_not_overwritten(env):
    captured = []
    class Client:
        def sell_orders(self, *args, **kwargs):
            return {"code": "OK"}
    def factory(s, sec):
        captured.append(sec.copy())
        return Client()
    mgr = manager(env, factory)
    aid = mgr.accounts.save(None, "Two", proxy="http://u:p@host:80", interval=5)
    a = mgr.accounts.list()[1]
    job = Job(aid, "Two", "owner", None)
    assert mgr._save(job, a, env[0], Context(), Page(), {}, "1.2.3.4", "ip:1.2.3.4")
    live = mgr.accounts.list()[1]
    assert live["BUFF_COOKIE"] == "session=NEWSESSION; csrf_token=CSRF"
    assert live["BUFF_CSRF"] == "CSRF" and live["egress_ip"] == "1.2.3.4"
    assert not live["enabled"] and captured[0]["BUFF_PROXY"] == "http://u:p@host:80"
    old = profile_key(live)
    mgr.accounts.save(aid, "Two", proxy="http://other:80", interval=5)
    with pytest.raises(ValueError, match="изменились"):
        mgr.accounts.receive_session(aid, old, {"BUFF_COOKIE": "bad"}, "1.1.1.1")
    assert mgr.accounts.list()[1]["BUFF_COOKIE"] != "bad"


def test_login_route_gate_honors_existing_cooldown(env):
    mgr = manager(env)
    env[0].pause_route("direct", (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
    a = mgr.accounts.list()[0]
    with pytest.raises(ValueError, match="паузу после 429"):
        mgr._prepare_route(Job("primary", "Main", "owner", None), a, env[0])


def test_proxy_ip_check_retries_transient_tunnel_failure_and_keeps_login(env, monkeypatch):
    mgr = manager(env)
    aid = mgr.accounts.save(None, "Proxy", proxy="http://private:SECRET@host:80", interval=5)
    a = mgr.accounts.list()[1]
    calls = []
    def get(url, **kw):
        calls.append(kw["proxies"])
        if kw["proxies"] and sum(bool(c) for c in calls) < 3:
            raise requests.exceptions.ProxyError("Temporary failure http://private:SECRET@host:80")
        return type("R", (), {"json": lambda self: {"ip": "1.2.3.4"}})()
    mgr.http_get = get
    job = Job(aid, "Proxy", "owner", ("user", "STEAMPASSWORD"))
    monkeypatch.setattr(job.stop, "wait", lambda seconds: False)
    ip, route, owner, _ = mgr._prepare_route(job, a, env[0])
    try:
        assert ip == "1.2.3.4" and sum(bool(c) for c in calls) == 3
        assert job.credentials == ("user", "STEAMPASSWORD")
        assert "SECRET" not in str(job.public())
    finally:
        env[0].release_route(route, owner)


@pytest.mark.parametrize("tunnel,status,attempts", [(True, 407, 1), (True, 502, 3), (False, 407, 1)])
def test_proxy_probe_errors_are_specific_bounded_and_secret_free(env, monkeypatch, tunnel, status, attempts):
    mgr = manager(env)
    calls = []
    def get(*args, **kw):
        calls.append(kw)
        if tunnel:
            raise requests.exceptions.ProxyError(f"http://private:SECRET@host:80 Tunnel connection failed: {status}")
        return type("R", (), {"status_code": status})()
    mgr.http_get = get
    job = Job("primary", "Main", "owner", None)
    monkeypatch.setattr(job.stop, "wait", lambda seconds: False)
    with pytest.raises(ValueError) as failure:
        mgr._egress_ip(job, "http://private:SECRET@host:80")
    assert len(calls) == attempts
    assert "SECRET" not in str(failure.value) and "private" not in str(failure.value)
    assert ("HTTP 407" if status == 407 else "3 попыток") in str(failure.value)


def test_proxy_probe_cancel_stops_retry_and_never_opens_browser(env, monkeypatch):
    mgr = manager(env)
    calls = []
    def get(*args, **kw):
        calls.append(kw)
        raise requests.exceptions.ProxyError("temporary")
    mgr.http_get = get
    job = Job("primary", "Main", "owner", None)
    monkeypatch.setattr(job.stop, "wait", lambda seconds: True)
    with pytest.raises(ValueError, match="отменён"):
        mgr._egress_ip(job, "http://host:80")
    assert len(calls) == 1


def test_browser_web_routes_enforce_csrf_ownership_and_secret_free_status(app):
    def runner(job, a):
        job.update(host="steamcommunity.com", image=b"FAKEIMAGE")
        job.stop.wait(5)
        job.credentials = None
        job.update(done=True, image=None)
    def factory(*args, **kwargs):
        return BrowserLogins(*args, **kwargs, browser_runner=runner)
    web = create_app(app.tmp / "buff.db", app.tmp / "settings.json", app.tmp / ".env", login_factory=factory)
    c = logged_in(web)
    assert c.post("/accounts/primary/login/start", data={"username": "alice", "password": "secretpass"}).status_code == 400
    token = csrf(c, "/accounts")
    response = c.post("/accounts/primary/login/start", data={"csrf": token, "username": "alice", "password": "secretpass"})
    path = response.headers["Location"]
    mgr = web.extensions["browser_logins"]
    try:
        body = c.get(path).get_data(as_text=True)
        status = c.get(path + "/status")
        assert status.status_code == 200 and status.headers["Cache-Control"] == "no-store"
        assert "secretpass" not in body + status.get_data(as_text=True)
        assert "alice" not in body + status.get_data(as_text=True)
        assert c.get(path + "/image").data == b"FAKEIMAGE"
        assert web.test_client().get(path + "/image").status_code == 302
        other = logged_in(web)
        assert other.get(path + "/status").status_code == 404
        assert c.post(path + "/action", data={"kind": "text", "text": "private"}).status_code == 400
        assert c.post(path + "/action", data={"csrf": token, "kind": "cancel"}).status_code == 200
    finally:
        mgr.close()


@pytest.mark.skipif(os.environ.get("BUFF_TEST_BROWSER") != "1", reason="Requires installed Chromium")
@pytest.mark.parametrize("navigation_interrupt", [False, True])
def test_real_chromium_login_flow_with_mocked_steam_and_buff(env, monkeypatch, navigation_interrupt):
    """Actual browser fills/submits the form, receives cookies, validates and saves.

    Every HTTP request is intercepted; no test credentials reach Steam/Buff.
    """
    from playwright import sync_api
    import traceback
    from notifier import browser_login
    diagnostics = []
    monkeypatch.setattr(browser_login.log, 'warning', lambda *args, **kw: diagnostics.append(traceback.format_exc()))
    actual = sync_api.sync_playwright
    submitted = []
    class Wrapped:
        def __enter__(self):
            self.real = actual()
            pw = self.real.__enter__()
            launch = pw.chromium.launch_persistent_context
            def patched(directory, **kwargs):
                ctx = launch(directory, **kwargs)
                def serve(route):
                    host = route.request.url.split('/')[2]
                    if host == 'steamcommunity.com':
                        if '/submit' in route.request.url:
                            submitted.append(route.request.post_data)
                            route.fulfill(content_type='text/html', body="<script>location.href='https://api.buff.market/callback'</script>")
                        else:
                            route.fulfill(content_type='text/html', body='''<form method="post" action="/submit">
                            <input name="username" type="text"><input name="password" type="password">
                            <button type="submit">Sign in</button></form>''')
                    elif host == 'api.buff.market':
                        ctx.add_cookies([dict(name='session',value='TESTBROWSER',domain='.buff.market',path='/',secure=True),
                                         dict(name='csrf_token',value='CSRF',domain='.buff.market',path='/',secure=True)])
                        route.fulfill(content_type='text/html', body="<script>location.href='https://buff.market/finish'</script>")
                    elif host == 'buff.market':
                        if '/finish' in route.request.url:
                            route.fulfill(content_type='text/html',body='<script>window.close()</script>')
                        else:
                            route.fulfill(content_type='text/html',body='''<button onclick="document.querySelector('#steam').hidden=false">Sign in</button>
                            <div id="steam" hidden onclick="window.open('https://steamcommunity.com/openid/loginform/')">Steam</div>''')
                    else:
                        route.abort()
                ctx.route('**/*',serve)
                if navigation_interrupt:
                    main_page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    original = main_page.get_by_text
                    interrupted = []
                    class RacingLocator:
                        def __init__(self, locator):
                            self.locator = locator
                        @property
                        def first(self):
                            return self
                        def count(self):
                            return self.locator.count()
                        def is_visible(self):
                            return self.locator.first.is_visible()
                        def click(self):
                            self.locator.first.click()
                            if not interrupted:
                                interrupted.append(True)
                                raise sync_api.Error("Execution context was destroyed, most likely because of a navigation")
                    main_page.get_by_text = lambda text, **kw: RacingLocator(original(text, **kw)) if text == 'Steam' else original(text, **kw)
                return ctx
            pw.chromium.launch_persistent_context = patched
            return pw
        def __exit__(self, *args):
            return self.real.__exit__(*args)
    monkeypatch.setattr(sync_api, 'sync_playwright', Wrapped)
    class Client:
        def sell_orders(self, *args, **kwargs):
            return {'code':'OK'}
    def client_factory(s, sec):
        assert sec['BUFF_COOKIE'] == 'session=TESTBROWSER; csrf_token=CSRF'
        return Client()
    mgr = manager(env, client_factory)
    job = mgr.start('primary','owner','TESTUSER','TESTPASSWORD')
    try:
        job.thread.join(timeout=35)
        assert job.done, job.public()
        assert job.state.startswith('Готово'), job.state + '\n' + '\n'.join(diagnostics)
        assert submitted == ['username=TESTUSER&password=TESTPASSWORD']
        assert read_env(env[2])['BUFF_COOKIE'] == 'session=TESTBROWSER; csrf_token=CSRF'
        assert job.credentials is None and job.image is None
    finally:
        mgr.close()
