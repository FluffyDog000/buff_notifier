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
from .accounts import Accounts, login_key, profile_key, route_keys, SESSION_KEYS
from .store import Store

log = logging.getLogger("buff.login")
WIDTH, HEIGHT = 1100, 760
LIFETIME = 600
REGISTRATION_RETRY_SECONDS = 5
REGISTRATION_MAX_ATTEMPTS = 3
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


def registration_form(page):
    """Find Next only inside the visible Buff Registration form, never pagination."""
    for heading in page.get_by_text("Registration", exact=True).all():
        if not heading.is_visible():
            continue
        container = heading.locator("xpath=ancestor::*[.//*[normalize-space(text())='Next']][1]")
        if container.count():
            for button in container.get_by_text("Next", exact=True).all():
                if button.is_visible() and button.is_enabled() and button.get_attribute("aria-disabled") != "true":
                    return True, button
        return True, None
    return False, None


class Job:
    def __init__(self, aid, label, owner, credentials):
        self.id, self.aid, self.label, self.owner = uuid.uuid4().hex, aid, label, owner
        self.lock = threading.Lock()
        self.actions = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.credentials = credentials
        self.login_hash = login_key(credentials[0]) if credentials else None
        self.created = time.monotonic()
        self.done, self.success, self.state, self.host, self.image = False, False, "Открываю браузер…", "", None
        self.thread = None
        self.saved_key = None

    def update(self, **values):
        with self.lock:
            for k, v in values.items():
                setattr(self, k, v)

    def public(self):
        with self.lock:
            return dict(id=self.id, account=self.aid, label=self.label, state=self.state,
                        host=self.host, done=self.done, success=self.success, has_image=self.image is not None,
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


class Batch:
    def __init__(self, entries, owner, auto_enable):
        self.id, self.owner, self.auto_enable = uuid.uuid4().hex, owner, auto_enable
        self.entries = entries
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.created, self.done, self.thread = time.monotonic(), False, None
        self.rows = [dict(id=r["id"], label=r["label"], created=r["created"], state="queued", message="В очереди", job=None) for r in entries]

    def update(self, index, **values):
        with self.lock:
            self.rows[index].update(values)

    def public(self):
        with self.lock:
            rows = [r.copy() for r in self.rows]
            return dict(id=self.id, done=self.done, auto_enable=self.auto_enable, rows=rows,
                        total=len(rows), created=sum(r["created"] for r in rows),
                        ok=sum(r["state"] in ("ok", "ready") for r in rows),
                        failed=sum(r["state"] == "error" for r in rows),
                        waiting=sum(r["state"] == "queued" for r in rows))

    def cancel(self):
        with self.lock:
            self.stop.set()
            for entry in self.entries:
                entry["password"] = ""


class BrowserLogins:
    def __init__(self, accounts: Accounts, store_path, settings_path, client_factory=buff.from_config,
                 http_get=requests.get, browser_runner=None):
        self.accounts, self.store_path, self.settings_path = accounts, Path(store_path), Path(settings_path)
        self.client_factory, self.http_get = client_factory, http_get
        self.runner = browser_runner or self._run
        self.lock, self.jobs = threading.Lock(), {}
        self.batches = {}
        self.closed = False

    def start(self, aid: str, owner: str, username="", password="", _batch_id=None) -> Job:
        a = next((a for a in self.accounts.list() if a["id"] == aid), None)
        if not a:
            raise ValueError("Аккаунт не найден.")
        browser_proxy(a["BUFF_PROXY"])
        if bool(username) != bool(password) or len(username) > 255 or len(password) > 512:
            raise ValueError("Укажите логин и пароль вместе либо оставьте оба поля пустыми.")
        with self.lock:
            if self.closed:
                raise ValueError("Служба перезапускается. Повторите вход позже.")
            if any(not b.done and b.id != _batch_id for b in self.batches.values()):
                raise ValueError("Сначала завершите или отмените массовый вход.")
            if any(not j.done for j in self.jobs.values()):
                raise ValueError("Сначала завершите или отмените уже открытое окно входа.")
            if username and any(p["id"] != aid and p.get("steam_login_hash") == login_key(username) for p in self.accounts.list()):
                raise ValueError("Этот Steam-логин уже привязан к другому профилю.")
            self.jobs = {jid: j for jid, j in self.jobs.items() if time.monotonic() - j.created < LIFETIME}
            job = Job(aid, a["label"], owner, (username, password) if username else None)
            self.jobs[job.id] = job
            job.thread = threading.Thread(target=self.runner, args=(job, a), daemon=True, name="buff-login")
            job.thread.start()
            return job

    def start_bulk(self, rows, owner, interval=None, auto_enable=True):
        with self.lock:
            if self.closed:
                raise ValueError("Служба перезапускается. Повторите импорт позже.")
            if any(not b.done for b in self.batches.values()) or any(not j.done for j in self.jobs.values()):
                raise ValueError("Сначала завершите или отмените открытый вход.")
            entries = self.accounts.import_bulk(rows, interval)
            batch = Batch(entries, owner, auto_enable)
            # Keep only compact completed summaries, never old credentials.
            completed = [b for b in self.batches.values() if b.done][-9:]
            self.batches = {b.id: b for b in completed}
            self.batches[batch.id] = batch
            batch.thread = threading.Thread(target=self._run_bulk, args=(batch,), daemon=True, name="buff-bulk-login")
            batch.thread.start()
            return batch

    def get_batch(self, bid, owner):
        with self.lock:
            batch = self.batches.get(bid)
            return batch if batch and batch.owner == owner else None

    def cancel_batch(self, batch):
        batch.cancel()
        with batch.lock:
            job_ids = {row["job"] for row in batch.rows}
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if not job.done and job.id in job_ids:
                job.stop.set()

    def owned_batches(self, owner):
        with self.lock:
            return [b for b in self.batches.values() if b.owner == owner]

    def _run_bulk(self, batch):
        store = None
        try:
            store = Store(self.store_path)
            for index, entry in enumerate(batch.entries):
                password = ""
                if batch.stop.is_set():
                    break
                try:
                    profile = next((a for a in self.accounts.list() if a["id"] == entry["id"]), None)
                    if not profile:
                        raise ValueError("Профиль удалён.")
                    if profile["BUFF_PROXY"] != entry["proxy"]:
                        raise ValueError("Прокси изменён после импорта. Повторите вход отдельно.")
                    expired = store.get_status(f"account:{profile['id']}:session_expired")
                    healthy = bool(profile["BUFF_COOKIE"] and profile.get("egress_ip") and
                                   store.get_status(f"account:{profile['id']}:session") == "работает" and
                                   expired != hashlib.sha256(profile["BUFF_COOKIE"].encode()).hexdigest()[:16])
                    if healthy:
                        if batch.auto_enable:
                            self.accounts.enable_verified(profile["id"], profile_key(profile))
                        entry["password"] = ""
                        batch.update(index, state="ready", message="Сессия уже проверена")
                        continue
                    batch.update(index, state="running", message="Открываю вход…")
                    with batch.lock:
                        password, entry["password"] = entry["password"], ""
                    if batch.stop.is_set():
                        break
                    job = self.start(entry["id"], batch.owner, entry["username"], password, _batch_id=batch.id)
                    del password
                    batch.update(index, job=job.id)
                    while not job.done:
                        if batch.stop.is_set():
                            job.stop.set()
                        batch.update(index, message=job.public()["state"])
                        job.thread.join(timeout=.5)
                    if batch.stop.is_set():
                        batch.update(index, state="cancelled", message="Вход отменён")
                        break
                    if job.success:
                        if batch.auto_enable:
                            self.accounts.enable_verified(entry["id"], job.saved_key)
                        batch.update(index, state="ok", message="Сессия проверена; опрос включён" if batch.auto_enable else "Сессия проверена")
                    else:
                        batch.update(index, state="error", message=job.public()["state"])
                except ValueError as e:
                    entry["password"] = ""
                    batch.update(index, state="error", message=str(e))
                except Exception as e:
                    entry["password"] = ""
                    log.warning("Ошибка массового входа %s: %s", entry["id"], type(e).__name__)
                    batch.update(index, state="error", message="Не удалось обработать аккаунт. Повторите вход отдельно.")
                finally:
                    password = ""
                    entry["password"] = ""
        finally:
            batch.cancel()
            with batch.lock:
                for row in batch.rows:
                    if row["state"] in ("queued", "running"):
                        row.update(state="cancelled", message="Очередь остановлена")
                batch.done = True
            if store:
                store.conn.close()

    def get(self, jid: str, owner: str) -> Job | None:
        with self.lock:
            job = self.jobs.get(jid)
            return job if job and job.owner == owner else None

    def close(self):
        with self.lock:
            self.closed = True
            batches, jobs = list(self.batches.values()), list(self.jobs.values())
        for batch in batches:
            self.cancel_batch(batch)
        for job in jobs:
            job.stop.set()
        for job in jobs:
            if job.thread:
                job.thread.join(timeout=25)
        for batch in batches:
            if batch.thread:
                batch.thread.join(timeout=5)

    def _prepare_route(self, job, a, store):
        proxy = a["BUFF_PROXY"]
        profiles = self.accounts.list()
        primary = profiles[0]
        if proxy and not primary["BUFF_PROXY"] and not primary.get("egress_ip"):
            direct = self._egress_ip(job, "")
            self.accounts.tested("primary", profile_key(primary), direct)
            store.link_routes("direct", "ip:" + direct)
        ip = self._egress_ip(job, proxy)
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

    def _egress_ip(self, job, proxy):
        """A transient proxy tunnel failure must not discard a queued login immediately."""
        for attempt in range(3):
            if job.stop.is_set():
                raise ValueError("Вход отменён.")
            job.update(state="Проверяю соединение и исходящий IP…" if not attempt else
                       f"Повторяю проверку соединения: попытка {attempt + 1} из 3…")
            try:
                response = self.http_get("https://api.ipify.org?format=json", timeout=15,
                                        proxies={"http": proxy, "https": proxy} if proxy else {})
                status = getattr(response, "status_code", 200)
                if status != 200:
                    if status == 407:
                        raise ValueError("Прокси отклонил авторизацию (HTTP 407). Проверьте его логин и пароль.")
                    raise ValueError(f"Проверка исходящего IP вернула HTTP {status}. Вход ещё не начат.")
                try:
                    return str(ipaddress.ip_address(response.json()["ip"]))
                except (ValueError, KeyError, TypeError):
                    raise ValueError("Не удалось определить исходящий IP. Вход ещё не начат.") from None
            except requests.RequestException as error:
                # Never expose requests/Playwright exception text: it can contain
                # the complete authenticated proxy URL. Inspect only fixed codes.
                if "Tunnel connection failed: 407" in str(error):
                    raise ValueError("Прокси отклонил авторизацию (HTTP 407). Проверьте его логин и пароль.") from None
                if attempt == 2:
                    raise ValueError("Не удалось соединиться для проверки IP после 3 попыток. "
                                     "Проверьте доступ прокси с сервера и повторите вход.") from None
                if job.stop.wait(attempt + 1):
                    raise ValueError("Вход отменён.") from None

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
        self.accounts.receive_session(a["id"], profile_key(a), values, ip, steam_login_hash=job.login_hash)
        store.set_status(f"account:{a['id']}:session", "работает")
        store.set_status(f"account:{a['id']}:session_expired", None)
        job.update(success=True, saved_key=profile_key(dict(a, **values)),
                   state="Готово: сессия BuffMarket сохранена и проверена. Можно включить аккаунт.")
        return True

    def _run(self, job, a):
        context, store, route, owner = None, None, None, None
        try:
            store = Store(self.store_path)
            from playwright.sync_api import Error as BrowserError, sync_playwright
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
                steam_access = {"denied": False}
                registration = {"attempts": 0, "at": 0, "limited": False}
                def observe(response):
                    try:
                        u = urlsplit(response.url)
                        if u.hostname in STEAM_HOSTS and response.status == 403 and response.request.is_navigation_request():
                            try:
                                main = response.frame.parent_frame is None
                            except BrowserError:  # popup navigation can precede its frame
                                main = True
                            if main:
                                steam_access["denied"] = True
                        if registration["attempts"] and response.status == 429 and (u.hostname == "buff.market" or (u.hostname or "").endswith(".buff.market")):
                            registration["limited"] = True
                            delay = buff.retry_after_seconds(response.headers.get("retry-after"))
                            until = (datetime.now(timezone.utc) + timedelta(seconds=delay if delay is not None else 900)).isoformat()
                            store.pause_route(route, until)
                            store.set_status(f"account:{a['id']}:pause_until", until)
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
                    try:
                        pages = [x for x in context.pages if not x.is_closed()]
                        if not pages:
                            job.update(state="Окно браузера закрыто. Начните вход заново.")
                            break
                        page = pages[-1]
                        now = time.monotonic()
                        if registration["limited"]:
                            raise ValueError("BuffMarket ограничил подтверждение регистрации (HTTP 429). Повторите вход после окончания паузы.")
                        if steam_access["denied"]:
                            raise ValueError("Steam отказал в доступе через этот прокси (HTTP 403, Access Denied). "
                                             "Вход не завершён. Проверьте доступ к Steam через этот прокси.")
                        host = urlsplit(page.url).hostname or ""
                        job.update(host=host)
                        if now - renewed_at > 30:
                            store.renew_claims(owner, datetime.now(timezone.utc))
                            renewed_at = now
                        if not allowed_page(page.url):
                            job.update(state="Ожидаю загрузку страницы Steam или BuffMarket…")
                        elif host in STEAM_HOSTS:
                            denied = page.get_by_role("heading", name="Access Denied", exact=True)
                            if denied.count() and denied.first.is_visible():
                                raise ValueError("Steam отказал в доступе через этот прокси (Access Denied). "
                                                 "Вход не завершён. Проверьте доступ к Steam через этот прокси.")
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
                                    submit_at = now
                                    page.locator('button[type="submit"]').first.click()
                                    job.update(state="Данные отправлены в Steam. Ожидаю завершения входа…")
                            elif now - submit_at > 3:
                                confirmation = page.locator('#imageLogin')
                                if confirmation.count() and confirmation.is_visible():
                                    submit_at = now
                                    confirmation.click()
                                    job.update(state="Подтверждаю вход в BuffMarket…")
                        elif host == "buff.market" or host.endswith(".buff.market"):
                            pending, next_button = registration_form(page)
                            if pending:
                                returned = True
                                if registration["attempts"] >= REGISTRATION_MAX_ATTEMPTS:
                                    job.update(state="Форма Registration осталась после 3 попыток. Можно нажать Next вручную в этом окне.")
                                elif next_button is not None and now - registration["at"] >= REGISTRATION_RETRY_SECONDS:
                                    registration["attempts"] += 1
                                    registration["at"] = now
                                    # Registration may finish without changing the session cookie.
                                    attempted_cookies.clear()
                                    job.update(state=f"Подтверждаю Registration: Next, попытка {registration['attempts']} из {REGISTRATION_MAX_ATTEMPTS}…")
                                    next_button.click()
                            cookies = context.cookies([buff.API])
                            cookie_key = hashlib.sha256(str(cookies).encode()).hexdigest()
                            has_session = any("session" in c["name"].lower() for c in cookies)
                            if not pending and host in ("buff.market", "www.buff.market") and (returned or has_session) and cookie_key not in attempted_cookies and now - probe_at >= max(5, interval):
                                attempted_cookies.add(cookie_key)
                                probe_at = now
                                if self._save(job, a, store, context, page, headers, ip, route, interval):
                                    (parent / "last_profile").write_text(identity, encoding="ascii")
                                    os.chmod(parent / "last_profile", 0o600)
                                    break
                            if not returned and now - signin_at > 5:
                                steam = page.get_by_text("Steam", exact=True)
                                signin = page.get_by_text("Sign in", exact=True)
                                signin_at = now
                                if steam.count() and steam.first.is_visible():
                                    steam.first.click()
                                elif signin.count() and signin.first.is_visible():
                                    signin.first.click()
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
                        except BrowserError:
                            # A popup can change/close while Chromium captures it.
                            # The screenshot is optional; keep checking the session.
                            job.update(image=None)
                            job.stop.wait(.2)
                            continue
                    except BrowserError as error:
                        # Steam's successful OpenID callback can close its popup
                        # while a locator or screenshot is still being evaluated.
                        # Continue on the surviving Buff page to save the session.
                        message = str(error)
                        if page.is_closed() or "Execution context was destroyed" in message or "Cannot find context with specified id" in message:
                            job.stop.wait(.1)
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
