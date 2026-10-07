"""Short-lived, private browser login jobs; no passwords or screenshots on disk.

Each Playwright context is accessed only by its owning thread. The web panel
gets a screenshot and submits a bounded set of actions, never cookies or CDP.
Persistent Chrome profiles belong to one account/proxy fingerprint. Buff
cookies are saved only after an authenticated API request succeeds.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from . import buff, config
from .accounts import Accounts, profile_key, route_keys, SESSION_KEYS
from .store import Store

log = logging.getLogger("buff.login")
WIDTH, HEIGHT = 1100, 760
LIFETIME = 600
STEAM_HOSTS = {"steamcommunity.com", "login.steampowered.com", "store.steampowered.com"}
KEYS = {"Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "Control+A",
        "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space"}


def allowed_page(url: str) -> bool:
    u = urlsplit(url)
    host = u.hostname or ""
    return u.scheme == "https" and (host in STEAM_HOSTS or host == "buff.market" or host.endswith(".buff.market"))


def browser_proxy(url: str) -> dict | None:
    if not url:
        return None
    u = urlsplit(url)
    scheme = "socks5" if u.scheme == "socks5h" else u.scheme
    if scheme == "socks5" and u.username is not None:
        raise ValueError("Для входа в браузере нужен HTTP/HTTPS-прокси или SOCKS5 без логина и пароля.")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    out = {"server": f"{scheme}://{host}:{u.port}"}
    if u.username is not None:
        out.update(username=unquote(u.username), password=unquote(u.password or ""))
    return out


class Job:
    def __init__(self, aid, label, owner, credentials):
        self.id, self.aid, self.label, self.owner = uuid.uuid4().hex, aid, label, owner
        self.lock = threading.Lock()
        self.actions = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.credentials = credentials
        self.created = time.monotonic()
        self.done, self.state, self.host, self.image = False, "Открываю браузер…", "", None
        self.thread = None

    def update(self, **values):
        with self.lock:
            for k, v in values.items():
                setattr(self, k, v)

    def public(self):
        with self.lock:
            return dict(id=self.id, account=self.aid, label=self.label, state=self.state,
                        host=self.host, done=self.done, has_image=self.image is not None,
                        width=WIDTH, height=HEIGHT)

    def action(self, kind, values):
        if self.done or self.stop.is_set():
            raise ValueError("Окно входа уже закрыто.")
        if kind == "cancel":
            self.stop.set()
            return
        if kind == "click":
            try:
                x, y = float(values["x"]), float(values["y"])
                if not math.isfinite(x + y) or not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                    raise ValueError
            except (KeyError, ValueError):
                raise ValueError("Клик вне окна браузера.") from None
            data = (kind, x, y)
        elif kind == "key" and values.get("key") in KEYS:
            data = (kind, values["key"])
        elif kind == "text" and 0 < len(values.get("text", "")) <= 1024:
            data = (kind, values["text"])
        elif kind in ("check", "buff", "scroll"):
            data = (kind,)
        else:
            raise ValueError("Недопустимое действие браузера.")
        try:
            self.actions.put_nowait(data)
        except queue.Full:
            raise ValueError("Дождитесь выполнения предыдущего действия.") from None


class BrowserLogins:
    def __init__(self, accounts: Accounts, store_path, settings_path, client_factory=buff.from_config,
                 http_get=requests.get, browser_runner=None):
        self.accounts, self.store_path, self.settings_path = accounts, Path(store_path), Path(settings_path)
        self.client_factory, self.http_get = client_factory, http_get
        self.runner = browser_runner or self._run
        self.lock, self.jobs = threading.Lock(), {}

    def start(self, aid: str, owner: str, username="", password="") -> Job:
        a = next((a for a in self.accounts.list() if a["id"] == aid), None)
        if not a:
            raise ValueError("Аккаунт не найден.")
        browser_proxy(a["BUFF_PROXY"])
        if bool(username) != bool(password) or len(username) > 255 or len(password) > 512:
            raise ValueError("Укажите логин и пароль вместе либо оставьте оба поля пустыми.")
        with self.lock:
            if any(not j.done for j in self.jobs.values()):
                raise ValueError("Сначала завершите или отмените уже открытое окно входа.")
            self.jobs = {jid: j for jid, j in self.jobs.items() if time.monotonic() - j.created < LIFETIME}
            job = Job(aid, a["label"], owner, (username, password) if username else None)
            self.jobs[job.id] = job
            job.thread = threading.Thread(target=self.runner, args=(job, a), daemon=True, name="buff-login")
            job.thread.start()
            return job

    def get(self, jid: str, owner: str) -> Job | None:
        with self.lock:
            job = self.jobs.get(jid)
            return job if job and job.owner == owner else None

    def close(self):
        for job in list(self.jobs.values()):
            job.stop.set()
        for job in list(self.jobs.values()):
            if job.thread:
                job.thread.join(timeout=25)

    def _prepare_route(self, job, a, store):
        proxy = a["BUFF_PROXY"]
        profiles = self.accounts.list()
        primary = profiles[0]
        if proxy and not primary["BUFF_PROXY"] and not primary.get("egress_ip"):
            direct = str(ipaddress.ip_address(self.http_get("https://api.ipify.org?format=json", timeout=15, proxies={}).json()["ip"]))
            self.accounts.tested("primary", profile_key(primary), direct)
            store.link_routes("direct", "ip:" + direct)
        ip = str(ipaddress.ip_address(self.http_get("https://api.ipify.org?format=json", timeout=15,
                        proxies={"http": proxy, "https": proxy} if proxy else {}).json()["ip"]))
        route = "ip:" + ip
        store.link_routes(route_keys([{k: v for k, v in a.items() if k != "egress_ip"}])[a["id"]], route)
        settings = config.load_settings(self.settings_path)
        profiles = self.accounts.list()
        routes = route_keys(profiles)
        interval = max([settings["request_interval"], a["interval"] or 0] +
                       [p["interval"] or settings["request_interval"] for p in profiles
                        if p["enabled"] and routes[p["id"]] == route])
        now = datetime.now(timezone.utc)
        pause = max(store.route_pause(route) or "", store.get_status(f"account:{a['id']}:pause_until") or "")
        if pause and datetime.fromisoformat(pause) > now:
            raise ValueError("Этот аккаунт или IP выдерживает паузу после 429. Начните вход после её окончания.")
        owner = "login:" + job.id
        for _ in range(48):
            if job.stop.is_set():
                raise ValueError("Вход отменён.")
            if store.acquire_route(route, owner, interval, datetime.now(timezone.utc)):
                return ip, route, owner, interval
            if job.stop.wait(0.25):
                raise ValueError("Вход отменён.")
        raise ValueError("Этот IP сейчас занят. Повторите вход чуть позже.")

    def _save(self, job, a, store, context, page, headers, ip, route, interval=None):
        cookies = context.cookies([buff.API])
        if not cookies:
            job.update(state="Сессия BuffMarket ещё не появилась. Завершите вход в Steam.")
            return False
        values = {"BUFF_COOKIE": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
                  "BUFF_CSRF": headers.get("x-csrftoken") or next((c["value"] for c in cookies if c["name"] in ("csrf_token", "csrftoken")), ""),
                  "BUFF_USER_AGENT": headers.get("user-agent") or page.evaluate("navigator.userAgent")}
        s, sec = config.load_settings(self.settings_path), config.load_secrets(self.accounts.env_path)
        s["request_interval"] = a["interval"] or s["request_interval"]
        sec.update({k: a[k] for k in SESSION_KEYS})
        sec.update(values)
        client = self.client_factory(s, sec)
        try:
            store.space_route(route, "login:" + job.id, interval or s["request_interval"], datetime.now(timezone.utc))
            gid = next((w["goods_id"] for w in store.watch_list()), 5777)
            client.sell_orders(gid, page_size=1)
        except buff.LoginRequired:
            job.update(state="Вход в BuffMarket пока не завершён. Продолжите в окне браузера.")
            return False
        except buff.BuffError as e:
            if e.status == 429:
                until = (datetime.now(timezone.utc) + timedelta(seconds=e.retry_after if e.retry_after is not None else 900)).isoformat()
                store.pause_route(route, until)
                store.set_status(f"account:{a['id']}:pause_until", until)
            raise ValueError(f"BuffMarket не принял проверку: HTTP {e.status or '—'}. Старая сессия сохранена.") from None
        finally:
            if hasattr(client, "session"):
                client.session.close()
        self.accounts.receive_session(a["id"], profile_key(a), values, ip)
        store.set_status(f"account:{a['id']}:session", "работает")
        store.set_status(f"account:{a['id']}:session_expired", None)
        job.update(state="Готово: сессия BuffMarket сохранена и проверена. Можно включить аккаунт.")
        return True

    def _run(self, job, a):
        context, store, route, owner = None, None, None, None
        try:
            store = Store(self.store_path)
            from playwright.sync_api import sync_playwright
            ip, route, owner, interval = self._prepare_route(job, a, store)
            parent = self.accounts.path.parent / "browser_profiles" / a["id"] / hashlib.sha256(a["BUFF_PROXY"].encode()).hexdigest()[:16]
            identity = hashlib.sha256(job.credentials[0].lower().encode()).hexdigest()[:32] if job.credentials else "manual"
            if not job.credentials and (parent / "last_profile").exists():
                previous = (parent / "last_profile").read_text(encoding="ascii").strip()
                if re.fullmatch(r"[0-9a-f]{32}|manual", previous):
                    identity = previous
            directory = parent / identity
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for private in (parent.parent.parent, parent.parent, parent, directory):
                os.chmod(private, 0o700)
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(str(directory), headless=True,
                    viewport={"width": WIDTH, "height": HEIGHT}, locale="en-US", proxy=browser_proxy(a["BUFF_PROXY"]),
                    accept_downloads=False, args=["--disable-save-password-bubble"])
                context.set_default_timeout(3000)
                context.set_default_navigation_timeout(20000)
                def restrict(r):
                    req = r.request
                    if req.is_navigation_request() and not allowed_page(req.url):
                        try:
                            main = req.frame.parent_frame is None
                        except Exception:  # first request of a popup has no frame yet
                            main = True
                        if main:
                            r.abort()
                            return
                    r.fallback()
                context.route("**/*", restrict)
                headers = {}
                def observe(response):
                    try:
                        u = urlsplit(response.url)
                        if u.hostname == "api.buff.market" and u.path == buff.SELL_ORDER:
                            headers.update(response.request.all_headers())
                    except Exception:  # response may disappear as a login popup closes
                        pass
                context.on("response", observe)
                page = context.pages[0] if context.pages else context.new_page()
                for old in context.pages[1:]:
                    old.close()
                page.goto("https://buff.market/", wait_until="domcontentloaded")
                headers["user-agent"] = page.evaluate("navigator.userAgent")
                signin_at, submit_at, probe_at, renewed_at, returned = 0, 0, 0, 0, False
                attempted_cookies = set()
                job.update(state="Открыт BuffMarket. Запускаю вход через Steam…")
                while not job.stop.is_set() and time.monotonic() - job.created < LIFETIME:
                    pages = [x for x in context.pages if not x.is_closed()]
                    if not pages:
                        job.update(state="Окно браузера закрыто. Начните вход заново.")
                        break
                    page = pages[-1]
                    now = time.monotonic()
                    host = urlsplit(page.url).hostname or ""
                    job.update(host=host)
                    if now - renewed_at > 30:
                        store.renew_claims(owner, datetime.now(timezone.utc))
                        renewed_at = now
                    if not allowed_page(page.url):
                        job.update(state="Ожидаю загрузку страницы Steam или BuffMarket…")
                    elif host in STEAM_HOSTS:
                        returned = True
                        if job.credentials:
                            username, password = job.credentials
                            name_field = page.locator('input[type="text"]').first
                            password_field = page.locator('input[type="password"]').first
                            if name_field.count() and password_field.count() and name_field.is_visible() and password_field.is_visible():
                                name_field.fill(username)
                                password_field.fill(password)
                                job.credentials = None
                                del username, password
                                page.locator('button[type="submit"]').first.click()
                                job.update(state="Данные отправлены в Steam. Ожидаю завершения входа…")
                                submit_at = now
                        elif now - submit_at > 3:
                            confirmation = page.locator('#imageLogin')
                            if confirmation.count() and confirmation.is_visible():
                                confirmation.click()
                                submit_at = now
                                job.update(state="Подтверждаю вход в BuffMarket…")
                    elif host == "buff.market" or host.endswith(".buff.market"):
                        cookies = context.cookies([buff.API])
                        cookie_key = hashlib.sha256(str(cookies).encode()).hexdigest()
                        has_session = any("session" in c["name"].lower() for c in cookies)
                        if (returned or has_session) and cookie_key not in attempted_cookies and now - probe_at >= max(5, interval):
                            attempted_cookies.add(cookie_key)
                            probe_at = now
                            if self._save(job, a, store, context, page, headers, ip, route, interval):
                                (parent / "last_profile").write_text(identity, encoding="ascii")
                                os.chmod(parent / "last_profile", 0o600)
                                break
                        if not returned and now - signin_at > 5:
                            steam = page.get_by_text("Steam", exact=True)
                            signin = page.get_by_text("Sign in", exact=True)
                            if steam.count() and steam.first.is_visible():
                                steam.first.click()
                            elif signin.count() and signin.first.is_visible():
                                signin.first.click()
                            signin_at = now
                    try:
                        action = job.actions.get_nowait()
                    except queue.Empty:
                        action = None
                    if action and allowed_page(page.url):
                        if action[0] == "click":
                            page.mouse.click(action[1], action[2])
                        elif action[0] == "key":
                            page.keyboard.press(action[1])
                        elif action[0] == "text":
                            page.keyboard.insert_text(action[1])
                        elif action[0] == "scroll":
                            page.mouse.wheel(0, 450)
                        elif action[0] == "buff":
                            page.goto("https://buff.market/", wait_until="domcontentloaded")
                            returned = True
                        elif action[0] == "check" and now - probe_at >= max(5, interval):
                            probe_at = now
                            if self._save(job, a, store, context, page, headers, ip, route, interval):
                                (parent / "last_profile").write_text(identity, encoding="ascii")
                                os.chmod(parent / "last_profile", 0o600)
                                break
                        action = None
                    if page.is_closed():
                        continue
                    try:
                        job.update(image=page.screenshot(type="jpeg", quality=75, timeout=5000))
                        page.wait_for_timeout(750)
                    except Exception:
                        if page.is_closed():
                            continue
                        raise
                else:
                    job.update(state="Вход отменён." if job.stop.is_set() else "Окно закрыто по таймауту. Начните вход заново.")
                context.close()
                context = None
        except ValueError as e:
            job.update(state=str(e))
        except Exception as e:
            log.warning("Ошибка браузерного входа аккаунта %s: %s", a["id"], type(e).__name__)
            job.update(state="Не удалось завершить вход. Проверьте прокси или повторите попытку.")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if store and route and owner:
                store.release_route(route, owner)
            if store:
                store.conn.close()
            job.credentials = None
            while not job.actions.empty():
                try:
                    job.actions.get_nowait()
                except queue.Empty:
                    break
            job.update(done=True, image=None)
