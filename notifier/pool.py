"""Parallel accounts, atomic work claims and a shared gate for each exit address."""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .accounts import Accounts, SESSION_KEYS, route_keys
from .poller import Poller, _hash
from .store import Store, now_iso

log = logging.getLogger("buff.pool")
MAX_IN_FLIGHT = 32
SCOPED = ("state", "session", "session_expired", "pause_until", "last_429", "session_alerted", "alerted_429", "heartbeat")


class AccountStore:
    def __init__(self, base: Store, aid: str, route: str):
        self.base, self.aid, self.route = base, aid, route

    def __getattr__(self, name):
        return getattr(self.base, name)

    def get_status(self, key):
        return self.base.get_status(f"account:{self.aid}:{key}" if key in SCOPED else key)

    def set_status(self, key, value):
        self.base.set_status(f"account:{self.aid}:{key}" if key in SCOPED else key, value)
        if key == "pause_until" and value:
            self.base.pause_route(self.route, value)

    def measurement(self, signature, interval_sec, proxy, now):
        return self.base.measurement(signature, interval_sec, proxy, now, self.aid)


class Worker(Poller):
    def __init__(self, store, profile, owner, settings_path, env_path, **kwargs):
        super().__init__(store, settings_path, env_path, work_owner=owner, **kwargs)
        self.profile = profile

    def context(self):
        settings, secrets = super().context()
        settings["request_interval"] = self.profile["interval"] or settings["request_interval"]
        secrets.update({k: self.profile[k] for k in SESSION_KEYS})
        return settings, secrets

    def alert_once(self, tg, key, marker, text):
        super().alert_once(tg, key, marker, f"Аккаунт «{self.profile['label']}»\n{text}")

    def close(self):
        if self._db is not None:
            self._db.close()
        if self._client is not None and hasattr(self._client, "session"):
            self._client.session.close()
        self.store.base.conn.close()


class Pool:
    def __init__(self, store_path: Path = config.STORE_PATH, settings_path: Path = config.SETTINGS_PATH,
                 env_path: Path = config.ENV_PATH, accounts_path: Path | None = None,
                 worker_factory=Worker):
        self.store_path, self.settings_path, self.env_path = Path(store_path), Path(settings_path), Path(env_path)
        self.store = Store(store_path)
        self.accounts = Accounts(accounts_path or Path(store_path).parent / "accounts.json", env_path)
        self.worker_factory = worker_factory
        self.executor = ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="buff-account")
        self.workers, self.futures, self.last_started = {}, {}, {}
        self.linked_routes = {}
        self.prefix = uuid.uuid4().hex + ":"
        self.last_renewed = None
        # Preserve the single-account cooldown/session state when upgrading.
        for key in SCOPED:
            scoped = f"account:primary:{key}"
            if self.store.get_status(scoped) is None:
                value = self.store.get_status(key)
                if value is not None:
                    self.store.set_status(scoped, value)
        self.store.set_status("pause_until", None)

    def tick(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        self.store.set_status("heartbeat", now_iso(now))
        for aid, (future, route, owner) in list(self.futures.items()):
            if not future.done():
                continue
            try:
                wait = future.result()
            except Exception:
                log.exception("Ошибка обработчика аккаунта %s", aid)
                wait = 60.0
            finally:
                self.store.release_route(route, owner)
                del self.futures[aid]
            if wait > 0:
                self.store.set_status(f"account:{aid}:next_at", (now + timedelta(seconds=wait)).isoformat())
        if self.last_renewed is None or now - self.last_renewed >= timedelta(seconds=30):
            self.store.renew_claims(self.prefix, now)
            self.last_renewed = now
        settings = config.load_settings(self.settings_path)
        profiles = self.accounts.list()
        routes = route_keys(profiles)
        unverified = route_keys([{k: v for k, v in a.items() if k != "egress_ip"} for a in profiles])
        for a in profiles:
            alias, target = unverified[a["id"]], routes[a["id"]]
            if self.linked_routes.get(alias) != target:
                self.store.link_routes(alias, target)
                self.linked_routes[alias] = target
        active = [a for a in profiles if a["enabled"] and a["BUFF_COOKIE"]]
        active_ids = {a["id"] for a in active}
        for aid in list(self.workers):
            if aid not in active_ids and aid not in self.futures:
                self.workers.pop(aid).close()
        if settings["paused"]:
            self.store.set_status("state", "пауза (настройки)")
            return 1.0
        self.store.set_status("state", f"работает: {len(active)} активных аккаунтов, обход по кругу" if active else "нет активной сессии BuffMarket")
        expired = sum(self.store.get_status(f"account:{a['id']}:session_expired") == _hash(a["BUFF_COOKIE"]) for a in active)
        cooling = sum(bool(self.store.route_pause(routes[a["id"]]) and
                           datetime.fromisoformat(self.store.route_pause(routes[a["id"]])) > now) for a in active)
        if expired or cooling:
            self.store.set_status("state", f"Включено аккаунтов: {len(active)}; сессий истекло: {expired}; в паузе после 429: {cooling}")
        self.store.set_status("session", "работает" if any(self.store.get_status(f"account:{a['id']}:session") == "работает" for a in active) else "не проверялась")
        if active and expired == len(active):
            self.store.set_status("session", "истекла")
        earliest = datetime.min.replace(tzinfo=timezone.utc)
        for profile in sorted(active, key=lambda a: self.last_started.get(a["id"], earliest)):
            aid, route = profile["id"], routes[profile["id"]]
            if aid in self.futures or len(self.futures) >= MAX_IN_FLIGHT:
                continue
            if self.store.get_status(f"account:{aid}:session_expired") == _hash(profile["BUFF_COOKIE"]):
                continue
            interval = profile["interval"] or settings["request_interval"]
            own_until = self.store.get_status(f"account:{aid}:pause_until")
            if own_until and datetime.fromisoformat(own_until) > now:
                self.store.pause_route(route, own_until)
                continue
            next_at = self.store.get_status(f"account:{aid}:next_at")
            if next_at and datetime.fromisoformat(next_at) > now:
                continue
            owner = self.prefix + aid
            route_interval = max(a["interval"] or settings["request_interval"] for a in active if routes[a["id"]] == route)
            if not self.store.acquire_route(route, owner, route_interval, now):
                continue
            try:
                if aid not in self.workers:
                    base = Store(self.store_path)
                    scoped = AccountStore(base, aid, route)
                    try:
                        self.workers[aid] = self.worker_factory(scoped, profile, owner, self.settings_path, self.env_path)
                    except Exception:
                        base.conn.close()
                        raise
                worker = self.workers[aid]
                worker.profile = profile
                worker.store.route = route
                self.store.set_status(f"account:{aid}:next_at", (now + timedelta(seconds=interval)).isoformat())
                self.last_started[aid] = now
                self.futures[aid] = (self.executor.submit(worker.cycle, now), route, owner)
            except Exception:
                self.store.release_route(route, owner)
                raise
        return 0.5

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        for _, route, owner in self.futures.values():
            self.store.release_route(route, owner)
        for worker in self.workers.values():
            worker.close()
        self.store.conn.close()
