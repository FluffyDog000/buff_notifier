"""The web page: settings, the BuffMarket session, one proxy, Telegram, the
list of polled items, and what the notifier has been doing.

It talks to the notifier only through files: `data/settings.json`, `.env`
and `data/buff.db`. The notifier re-reads them every cycle, so a change here
applies on its next item without a restart.

Public through Caddy and DuckDNS, so: one password (its hash in `.env`),
five wrong tries lock an address for 15 minutes, every form carries a CSRF
token, the session cookie is Secure and HttpOnly. Secrets are never sent
back to the page - only their last characters.
"""
from __future__ import annotations

import hmac
import hashlib
import ipaddress
import logging
import secrets as pysecrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from notifier import buff, candidates, config, sales
from notifier.buff import BuffError, LoginRequired
from notifier.accounts import Accounts, profile_key, route_keys, SESSION_KEYS
from notifier.browser_login import BrowserLogins
from notifier.bulk_accounts import parse_bulk
from notifier.envfile import write_env
from notifier.listings import item_url
from notifier.poller import daily_load
from notifier.store import Store
from notifier.telegram import Telegram
from tools.set_session import parse_curl

log = logging.getLogger("buff.web")

MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
LOOKUPS_PER_SUBMIT = 10
TEST_GOODS_ID = 5777          # Desert Eagle | Mecha Industries (MW): any item will do


def create_app(store_path: Path = config.STORE_PATH, settings_path: Path = config.SETTINGS_PATH,
               env_path: Path = config.ENV_PATH, client_factory=buff.from_config,
               telegram_factory=Telegram, http_get=requests.get, accounts_path: Path | None = None,
               login_factory=BrowserLogins) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    sec = config.load_secrets(env_path)
    if not sec["WEB_SECRET_KEY"]:
        sec["WEB_SECRET_KEY"] = pysecrets.token_hex(32)
        write_env(env_path, {"WEB_SECRET_KEY": sec["WEB_SECRET_KEY"]})
    app.config.update(SECRET_KEY=sec["WEB_SECRET_KEY"], SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=1024 * 1024)
    store = Store(store_path)
    accounts = Accounts(accounts_path or Path(store_path).parent / "accounts.json", env_path)
    logins = login_factory(accounts, store_path, settings_path, client_factory=client_factory, http_get=http_get)
    app.extensions["browser_logins"] = logins
    fails: dict[str, list[float]] = {}

    def settings() -> dict:
        return config.load_settings(settings_path)

    def secrets() -> dict:
        return config.load_secrets(env_path)

    def client_ip() -> str:
        return request.remote_addr or "?"

    # -- guard ------------------------------------------------------------------

    @app.before_request
    def guard():
        if request.endpoint == "static":
            return None
        if not secrets()["WEB_PASSWORD_HASH"]:
            return render_template("setup.html"), 503
        if request.method == "POST":
            token = request.form.get("csrf", "")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                abort(400, "Форма устарела, обновите страницу.")
        if request.endpoint != "login" and not session.get("ok"):
            return redirect(url_for("login"))
        return None

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = pysecrets.token_urlsafe(32)
        return {"csrf": session["csrf"], "mask": config.mask, "ago": ago}

    # -- login ------------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html")
        ip = client_ip()
        rec = fails.get(ip, [0, 0.0])
        if rec[1] > time.time():
            return render_template("login.html", error=f"Слишком много попыток, подождите "
                                   f"{int(rec[1] - time.time()) // 60 + 1} мин."), 429
        if check_password_hash(secrets()["WEB_PASSWORD_HASH"], request.form.get("password", "")):
            fails.pop(ip, None)
            csrf = session.get("csrf")
            session.clear()
            session.update(ok=True, csrf=csrf or pysecrets.token_urlsafe(32))
            session.permanent = True
            return redirect(url_for("index"))
        rec[0] += 1
        if rec[0] >= MAX_FAILS:
            rec[:] = [0, time.time() + LOCK_SECONDS]
            log.warning("Вход заблокирован на 15 минут для %s", ip)
        fails[ip] = rec
        return render_template("login.html", error="Неверный пароль."), 401

    @app.route("/logout", methods=["POST"])
    def logout():
        owner = hashlib.sha256(session.get("csrf", "").encode()).hexdigest()
        for batch in logins.owned_batches(owner):
            logins.cancel_batch(batch)
        with logins.lock:
            jobs = list(logins.jobs.values())
        for job in jobs:
            if job.owner == owner:
                job.stop.set()
        session.clear()
        return redirect(url_for("login"))

    # -- overview ---------------------------------------------------------------

    @app.route("/")
    def index():
        s, st = settings(), store.status()
        watch = store.watch_list()
        need, allowed = daily_load(watch, s)
        try:
            profiles = accounts.list()
        except ValueError:
            profiles = []
        active_accounts = [a for a in profiles if a["enabled"] and a["BUFF_COOKIE"]]
        route_map = route_keys(profiles)
        capacities = {}
        for a in active_accounts:
            route = route_map[a["id"]]
            capacities[route] = max(capacities.get(route, 0), a["interval"] or s["request_interval"])
        throughput = sum(1 / interval for interval in capacities.values())
        allowed = 86400 * throughput
        beat = st.get("heartbeat", {}).get("value")
        alive = bool(beat) and datetime.now(timezone.utc) - datetime.fromisoformat(beat) < timedelta(minutes=3)
        return render_template("index.html", st=st, alive=alive, beat=beat,
                               db=csfloat_info(s["csfloat_db"]), need=need, allowed=allowed,
                               active=sum(1 for w in watch if w["active"]),
                               account_count=len(active_accounts), route_count=len(capacities),
                               total_hour=store.total_requests(), total_five=store.total_requests(minutes=5),
                               scan_min_minutes=sum(1 for w in watch if w["active"]) / throughput / 60 if throughput else None,
                               signals=store.recent_signals(100), s=s, metrics=store.measurement_stats())

    # -- parallel account profiles --------------------------------------------

    @app.route("/accounts")
    def accounts_page():
        s = settings()
        try:
            profiles = accounts.list()
        except ValueError as e:
            flash(str(e), "error")
            profiles = []
        rows = []
        routes = route_keys(profiles)
        for a in profiles:
            aid = a["id"]
            metrics = store.measurement_stats(account_id=aid)["current"]
            rows.append({"id": aid, "label": a["label"], "enabled": a["enabled"],
                         "interval": a["interval"] or s["request_interval"],
                         "proxy": buff.proxy_display(a["BUFF_PROXY"]), "has_cookie": bool(a["BUFF_COOKIE"]),
                         "ip": a.get("egress_ip"), "session_state": store.get_status(f"account:{aid}:session") or "не проверялась",
                         "pause_until": max(store.get_status(f"account:{aid}:pause_until") or "",
                                            store.route_pause(routes[aid]) or "") or None, "metrics": metrics})
        batches = [b.public() for b in logins.owned_batches(browser_owner())]
        response = app.make_response(render_template("accounts.html", accounts=rows,
                                    batches=batches, default_interval=s["request_interval"]))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/accounts/save", methods=["POST"])
    def accounts_save():
        try:
            text = request.form.get("curl", "").strip()
            values = parse_curl(text) if text else None
            if text and not (values or {}).get("BUFF_COOKIE"):
                raise ValueError("В curl нет сессии. Нужен GET sell_order из браузера после входа.")
            if values:
                values = {k: values.get(k, "") for k in ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT")}
            raw = request.form.get("interval", "").strip()
            interval = float(raw.replace(",", ".")) if raw else None
            proxy = request.form.get("proxy", "").strip() or None
            if request.form.get("clear_proxy"):
                proxy = ""
            aid = accounts.save(request.form.get("id") or None, request.form.get("label", ""), values, proxy, interval)
            flash("Аккаунт сохранён. Новый или изменённый дополнительный аккаунт сначала проверьте, затем включите.", "ok")
        except ValueError as e:
            flash(str(e) if "could not convert" not in str(e) and "No closing quotation" not in str(e)
                  else "Не разобрал ввод. Проверьте curl и числовую паузу.", "error")
        return redirect(url_for("accounts_page"))

    @app.route("/accounts/<aid>/toggle", methods=["POST"])
    def accounts_toggle(aid):
        try:
            accounts.toggle(aid)
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("accounts_page"))

    @app.route("/accounts/<aid>/remove", methods=["POST"])
    def accounts_remove(aid):
        try:
            accounts.remove(aid)
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("accounts_page"))

    @app.route("/accounts/<aid>/test", methods=["POST"])
    def accounts_test(aid):
        owner, route = "test:" + uuid.uuid4().hex, None
        try:
            profiles = accounts.list()
            a = next((p for p in profiles if p["id"] == aid), None)
            if not a or not a["BUFF_COOKIE"]:
                raise ValueError("Сначала сохраните сессию аккаунта.")
            key = profile_key(a)
            s, sec = settings(), secrets()
            s["request_interval"] = a["interval"] or s["request_interval"]
            sec.update({k: a[k] for k in SESSION_KEYS})
            # Learn the direct route too, so a proxy with the same exit IP cannot
            # be mistaken for a separate source of capacity.
            primary = profiles[0]
            if a["BUFF_PROXY"] and not primary["BUFF_PROXY"] and not primary.get("egress_ip"):
                direct_ip = str(ipaddress.ip_address(http_get("https://api.ipify.org?format=json", timeout=15, proxies={}).json()["ip"]))
                accounts.tested("primary", profile_key(primary), direct_ip)
                store.link_routes("direct", "ip:" + direct_ip)
            proxy = a["BUFF_PROXY"]
            ip = str(ipaddress.ip_address(http_get("https://api.ipify.org?format=json", timeout=15,
                          proxies={"http": proxy, "https": proxy} if proxy else {}).json()["ip"]))
            profiles = accounts.list()
            a["egress_ip"] = ip
            prospective = [a if p["id"] == aid else p for p in profiles]
            route = route_keys(prospective)[aid]
            store.link_routes(route_keys([{k: v for k, v in a.items() if k != "egress_ip"}])[aid], route)
            now = datetime.now(timezone.utc)
            own_pause = store.get_status(f"account:{aid}:pause_until")
            if own_pause and datetime.fromisoformat(own_pause) > now:
                raise ValueError("Аккаунт ещё выдерживает паузу после 429; дождитесь её окончания.")
            interval = max([s["request_interval"]] + [p["interval"] or settings()["request_interval"] for p in prospective
                           if p["enabled"] and route_keys(prospective)[p["id"]] == route])
            if not store.acquire_route(route, owner, interval, now):
                raise ValueError("Этот IP занят или выдерживает паузу; повторите проверку позже.")
            gid = next((w["goods_id"] for w in store.watch_list()), TEST_GOODS_ID)
            client = client_factory(s, sec)
            try:
                client.sell_orders(gid, page_size=1)
            finally:
                if hasattr(client, "session"):
                    client.session.close()
            accounts.tested(aid, key, ip)
            store.set_status(f"account:{aid}:session", "работает")
            store.set_status(f"account:{aid}:session_expired", None)
            flash(f"Сессия работает. Исходящий IP: {ip}. Можно включить аккаунт.", "ok")
        except LoginRequired:
            store.set_status(f"account:{aid}:session", "истекла")
            flash("Сессия истекла; сохраните свежий curl.", "error")
        except BuffError as e:
            if e.status == 429 and route:
                until = (datetime.now(timezone.utc) + timedelta(seconds=e.retry_after if e.retry_after is not None else 900)).isoformat()
                store.pause_route(route, until)
                store.set_status(f"account:{aid}:pause_until", until)
            flash(f"BuffMarket не принял проверку: HTTP {e.status or '—'}.", "error")
        except requests.RequestException:
            flash("Сеть или прокси не отвечают; аккаунт не проверен.", "error")
        except (ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, ValueError) else "Не удалось определить IP.", "error")
        finally:
            if route:
                store.release_route(route, owner)
        return redirect(url_for("accounts_page"))

    # -- items ------------------------------------------------------------------

    def browser_owner():
        return hashlib.sha256(session.get("csrf", "").encode()).hexdigest()

    def browser_job(jid):
        job = logins.get(jid, browser_owner())
        if job is None:
            abort(404)
        return job

    def browser_batch(bid):
        batch = logins.get_batch(bid, browser_owner())
        if batch is None:
            abort(404)
        return batch

    @app.route("/accounts/bulk", methods=["POST"])
    def accounts_bulk_start():
        try:
            rows = parse_bulk(request.form.get("accounts", ""), request.form.get("proxies", ""),
                              shared_proxy=bool(request.form.get("shared_proxy")))
            raw = request.form.get("interval", "").strip()
            try:
                interval = float(raw.replace(",", ".")) if raw else None
            except ValueError:
                raise ValueError("Пауза должна быть числом от 1 до 600 секунд.") from None
            batch = logins.start_bulk(rows, browser_owner(), interval,
                                      auto_enable=bool(request.form.get("auto_enable")))
            return redirect(url_for("accounts_bulk", bid=batch.id))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("accounts_page"))

    @app.route("/accounts/bulk/<bid>")
    def accounts_bulk(bid):
        response = app.make_response(render_template("bulk_accounts.html", batch=browser_batch(bid).public()))
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return response

    @app.route("/accounts/bulk/<bid>/status")
    def accounts_bulk_status(bid):
        response = jsonify(browser_batch(bid).public())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/accounts/bulk/<bid>/cancel", methods=["POST"])
    def accounts_bulk_cancel(bid):
        logins.cancel_batch(browser_batch(bid))
        return redirect(url_for("accounts_bulk", bid=bid))

    def start_browser(aid):
        try:
            job = logins.start(aid, browser_owner(), request.form.get("username", "").strip(), request.form.get("password", ""))
            return redirect(url_for("accounts_browser", jid=job.id))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("accounts_page"))

    @app.route("/accounts/<aid>/login/start", methods=["POST"])
    def accounts_login_start(aid):
        return start_browser(aid)

    @app.route("/accounts/login/new", methods=["POST"])
    def accounts_login_new():
        try:
            raw = request.form.get("interval", "").strip()
            interval = float(raw.replace(",", ".")) if raw else None
            aid = accounts.save(None, request.form.get("label", ""), proxy=request.form.get("proxy", ""), interval=interval)
        except ValueError as e:
            flash(str(e) if "could not convert" not in str(e) else "Пауза должна быть числом.", "error")
            return redirect(url_for("accounts_page"))
        return start_browser(aid)

    @app.route("/accounts/login/<jid>")
    def accounts_browser(jid):
        response = app.make_response(render_template("browser_login.html", job=browser_job(jid).public()))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.route("/accounts/login/<jid>/status")
    def accounts_browser_status(jid):
        response = jsonify(browser_job(jid).public())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/accounts/login/<jid>/image")
    def accounts_browser_image(jid):
        job = browser_job(jid)
        with job.lock:
            data = job.image
        return Response(data or b"", status=200 if data else 204, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.route("/accounts/login/<jid>/action", methods=["POST"])
    def accounts_browser_action(jid):
        try:
            browser_job(jid).action(request.form.get("kind"), request.form)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    # -- items ------------------------------------------------------------------

    @app.route("/items")
    def items():
        s = settings()
        pick_args = {"min_rate": request.args.get("min_rate", "0"),
                     "min_price": request.args.get("min_price", "5"),
                     "limit": request.args.get("limit", "50")}
        picked = None
        if request.args.get("pick"):
            try:
                watched = {w["name"] for w in store.watch_list()}
                watched |= {p["name"] for p in store.pending_list()}
                conn = sales.connect(s["csfloat_db"])
                picked = candidates.pick(conn, s, float(pick_args["min_rate"].replace(",", ".")),
                                         float(pick_args["min_price"].replace(",", ".")),
                                         int(pick_args["limit"]), exclude=watched)
                conn.close()
            except (ValueError, sqlite3.Error) as e:
                flash(f"Не подобрал: {e}", "error")
        return render_template("items.html", watch=store.watch_list(), names=csfloat_names(s),
                               pending=store.pending_list(), picked=picked, pick=pick_args,
                               url=item_url)

    @app.route("/items/queue", methods=["POST"])
    def items_queue():
        names = [n for n in request.form.get("names", "").splitlines() if n.strip()]
        n = store.add_pending(names)
        flash(f"В очередь поиска: {n}. Сервис найдёт goods_id сам, по одному запросу "
              f"в паузах между опросами.", "ok")
        return redirect(url_for("items"))

    @app.route("/items/pending/remove", methods=["POST"])
    def pending_remove():
        store.remove_pending(request.form.get("name") or None)
        return redirect(url_for("items"))

    @app.route("/items/add", methods=["POST"])
    def items_add():
        s, sec = settings(), secrets()
        lines = [ln.strip() for ln in request.form.get("lines", "").splitlines() if ln.strip()]
        known = set(csfloat_names(s))
        added, lookups, client, queued = [], 0, None, []
        for ln in lines:
            gid_text, _, name = ln.partition(";")
            try:
                gid = int(gid_text.strip())
            except ValueError:
                # A bare name: its goods_id is for the notifier to find.
                if known and ln not in known:
                    flash(f"«{ln}» нет среди предметов CSFloat-бота.", "error")
                else:
                    queued.append(ln)
                continue
            name = name.strip()
            if not name:
                if lookups >= LOOKUPS_PER_SUBMIT:
                    flash(f"{gid}: без названия — не больше {LOOKUPS_PER_SUBMIT} за раз", "error")
                    continue
                lookups += 1
                try:
                    client = client or client_factory(s, sec)
                    name = buff.goods_name(client.sell_orders(gid, page_size=1), gid) or ""
                except (BuffError, requests.RequestException) as e:
                    flash(f"{gid}: не узнал название на BuffMarket ({e}). Впишите его: "
                          f"«{gid};название».", "error")
                    continue
                if not name:
                    flash(f"{gid}: BuffMarket не назвал предмет. Впишите название.", "error")
                    continue
            if known and name not in known:
                flash(f"{gid}: «{name}» нет среди предметов CSFloat-бота — оценивать не по чему. "
                      "Для фаз Doppler укажите название с фазой.", "error")
                continue
            store.add_watch(gid, name)
            added.append(name)
        if added:
            flash(f"Добавлено: {', '.join(added)}", "ok")
        if queued:
            flash(f"В очередь поиска goods_id: {store.add_pending(queued)}.", "ok")
        return redirect(url_for("items"))

    @app.route("/items/<int:gid>/toggle", methods=["POST"])
    def items_toggle(gid):
        row = next((w for w in store.watch_list() if w["goods_id"] == gid), None)
        if row:
            store.set_active(gid, not row["active"])
        return redirect(url_for("items"))

    @app.route("/items/<int:gid>/delete", methods=["POST"])
    def items_delete(gid):
        store.remove_watch(gid)
        return redirect(url_for("items"))

    # -- settings ---------------------------------------------------------------

    @app.route("/settings")
    def settings_page():
        sec = secrets()
        return render_template("settings.html", s=settings(), fields=config.FIELDS,
                               groups=list(dict.fromkeys(f.group for f in config.FIELDS)),
                               sec=sec, proxy=buff.proxy_display(sec["BUFF_PROXY"]),
                               session_state=store.get_status("session") or "не проверялась",
                               errors={})

    @app.route("/settings", methods=["POST"])
    def settings_save():
        values, errors = config.parse_form(request.form)
        if errors:
            sec = secrets()
            flash("Не сохранено: проверьте отмеченные поля.", "error")
            return render_template("settings.html", s={**settings(), **values}, fields=config.FIELDS,
                                   groups=list(dict.fromkeys(f.group for f in config.FIELDS)),
                                   sec=sec, proxy=buff.proxy_display(sec["BUFF_PROXY"]),
                                   session_state=store.get_status("session") or "не проверялась",
                                   errors=errors), 400
        config.save_settings(values, settings_path)
        flash("Настройки сохранены. Сервис подхватит их на следующем предмете.", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/settings/session", methods=["POST"])
    def session_save():
        try:
            values = parse_curl(request.form.get("curl", ""))
        except ValueError:
            values = {}
        if "BUFF_COOKIE" not in values:
            flash("В тексте нет кук. Нужен «Copy as cURL» запроса sell_order из браузера, "
                  "где вы вошли на buff.market.", "error")
            return redirect(url_for("settings_page"))
        write_env(env_path, {k: values.get(k, "") for k in ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT")})
        store.set_status("session", "новая, не проверена")
        names = [c.split("=", 1)[0].strip() for c in values["BUFF_COOKIE"].split(";") if c.strip()]
        flash(f"Сессия сохранена (куки: {', '.join(names)}). Нажмите «Проверить».", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/settings/session/test", methods=["POST"])
    def session_test():
        s, sec = settings(), secrets()
        gid = next((w["goods_id"] for w in store.watch_list()), TEST_GOODS_ID)
        try:
            body = client_factory(s, sec).sell_orders(gid, page_size=1)
            store.set_status("session", "работает")
            flash(f"Сессия работает: BuffMarket ответил, предмет {buff.goods_name(body, gid) or gid}.", "ok")
        except LoginRequired:
            store.set_status("session", "истекла")
            flash("BuffMarket: нужен вход. Сессия пуста или истекла — вставьте свежий curl.", "error")
        except (BuffError, requests.RequestException) as e:
            flash(f"Не получилось: {e}", "error")
        return redirect(url_for("settings_page"))

    @app.route("/settings/proxy", methods=["POST"])
    def proxy_save():
        url = request.form.get("proxy", "").strip()
        if request.form.get("clear"):
            write_env(env_path, {"BUFF_PROXY": None})
            flash("Прокси убран: запросы идут с адреса сервера.", "ok")
        elif url:
            if "://" not in url or url.split("://", 1)[0] not in ("http", "https", "socks5", "socks5h"):
                flash("Формат: http://логин:пароль@host:port или socks5://…", "error")
            else:
                write_env(env_path, {"BUFF_PROXY": url})
                flash(f"Прокси сохранён: {buff.proxy_display(url)}. Нажмите «Проверить».", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/settings/proxy/test", methods=["POST"])
    def proxy_test():
        p = secrets()["BUFF_PROXY"]
        try:
            r = http_get("https://api.ipify.org?format=json", timeout=15,
                         proxies={"http": p, "https": p} if p else None)
            flash(f"Адрес, с которого BuffMarket видит запросы: {r.json().get('ip')}"
                  + ("" if p else " (без прокси, адрес сервера)"), "ok")
        except (requests.RequestException, ValueError) as e:
            flash(f"Прокси не отвечает: {str(e)[:200]}", "error")
        return redirect(url_for("settings_page"))

    @app.route("/settings/telegram", methods=["POST"])
    def telegram_save():
        changes = {}
        token = request.form.get("token", "").strip()
        if token:
            changes["TELEGRAM_BOT_TOKEN"] = token
        if "chat_id" in request.form:
            changes["TELEGRAM_CHAT_ID"] = request.form["chat_id"].strip()
        if changes:
            write_env(env_path, changes)
            flash("Telegram сохранён.", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/settings/telegram/test", methods=["POST"])
    def telegram_test():
        sec = secrets()
        tg = telegram_factory(sec["TELEGRAM_BOT_TOKEN"], sec["TELEGRAM_CHAT_ID"])
        if not tg.configured():
            flash("Нужны и токен, и chat id.", "error")
        elif tg.send("✅ Проверка: уведомления BuffMarket приходят сюда."):
            flash("Отправлено — проверьте Telegram.", "ok")
        else:
            flash("Telegram не принял сообщение: проверьте токен и chat id, и что вы написали боту /start.", "error")
        return redirect(url_for("settings_page"))

    @app.route("/settings/telegram/chats", methods=["POST"])
    def telegram_chats():
        sec = secrets()
        try:
            chats = telegram_factory(sec["TELEGRAM_BOT_TOKEN"], "").chats()
        except RuntimeError as e:
            flash(f"Telegram: {e}", "error")
            return redirect(url_for("settings_page"))
        if not chats:
            flash("Боту ещё никто не писал. Напишите ему /start и нажмите снова.", "error")
        for c in chats:
            flash(f"Чат {c['id']} — {c['title']}", "ok")
        return redirect(url_for("settings_page"))

    return app


def csfloat_info(path: str) -> dict:
    try:
        if not Path(path).exists():
            return {"ok": False, "error": "файла нет", "path": path}
        conn = sales.connect(path)
        items = conn.execute("SELECT COUNT(*) FROM items WHERE active = 1").fetchone()[0]
        last = conn.execute("SELECT MAX(sold_at) FROM sales").fetchone()[0]
        conn.close()
        return {"ok": True, "path": path, "count": items, "last_sale": last}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e), "path": path}


def csfloat_names(s: dict) -> list[str]:
    try:
        if not Path(s["csfloat_db"]).exists():
            return []
        conn = sales.connect(s["csfloat_db"])
        out = [n for _, n in sales.active_items(conn)]
        conn.close()
        return sorted(out)
    except sqlite3.Error:
        return []


def ago(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        sec = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return iso
    if sec < 0:
        m = int(-sec // 60)
        return f"через {m} мин" if m < 120 else f"через {m // 60} ч"
    if sec < 90:
        return "только что"
    if sec < 5400:
        return f"{int(sec // 60)} мин назад"
    if sec < 172800:
        return f"{int(sec // 3600)} ч назад"
    return f"{int(sec // 86400)} дн назад"
