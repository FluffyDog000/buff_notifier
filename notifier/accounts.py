"""Private account profiles. The original account remains in .env."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .envfile import write_env

ACCOUNT_PATH = config.DATA / "accounts.json"
LOCK = threading.Lock()
SESSION_KEYS = ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT", "BUFF_PROXY")


def login_key(username: str) -> str:
    return hashlib.sha256(username.lower().encode()).hexdigest()


def profile_key(account: dict) -> str:
    return hashlib.sha256(json.dumps([account.get(k, "") for k in SESSION_KEYS]).encode()).hexdigest()


def validate_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        u = urlsplit(value)
        if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname or not u.port:
            raise ValueError
    except ValueError:
        raise ValueError("Прокси: http://логин:пароль@host:port или socks5://…") from None
    return value


def route_keys(profiles: list[dict]) -> dict[str, str]:
    """Known equal exit IPs share a gate. Unverified proxy endpoints share by host/port."""
    direct_ip = next((a.get("egress_ip") for a in profiles if not a["BUFF_PROXY"] and a.get("egress_ip")), None)
    out = {}
    for a in profiles:
        proxy = a["BUFF_PROXY"]
        if not proxy:
            route = "ip:" + direct_ip if direct_ip else "direct"
        elif a.get("egress_ip"):
            route = "ip:" + a["egress_ip"]
        else:
            u = urlsplit(proxy)
            route = "endpoint:" + hashlib.sha256(f"{u.hostname}:{u.port}".encode()).hexdigest()[:24]
        out[a["id"]] = route
    return out


class Accounts:
    def __init__(self, path: Path = ACCOUNT_PATH, env_path: Path = config.ENV_PATH):
        self.path, self.env_path = Path(path), Path(env_path)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) > 200:
                raise ValueError
            ids = set()
            for a in data:
                if (not isinstance(a, dict) or not isinstance(a.get("id"), str)
                        or a["id"] in ids or not isinstance(a.get("label"), str)
                        or not isinstance(a.get("enabled"), bool)
                        or (a.get("interval") is not None and
                            (not isinstance(a["interval"], (int, float)) or not math.isfinite(a["interval"])
                             or not 1 <= a["interval"] <= 600))
                        or any(not isinstance(a.get(k, ""), str) for k in SESSION_KEYS)):
                    raise ValueError
                validate_proxy(a.get("BUFF_PROXY", ""))
                if a.get("egress_ip"):
                    ipaddress.ip_address(a["egress_ip"])
                ids.add(a["id"])
            return data
        except (OSError, ValueError):
            raise ValueError("Файл аккаунтов недоступен или повреждён; опрос остановлен.") from None

    def _write(self, profiles: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="accounts-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(profiles, f, ensure_ascii=False, indent=1)
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def list(self) -> list[dict]:
        stored = self._read()
        primary = {"id": "primary", "label": "Основной", "enabled": True, "interval": None}
        primary.update(next((a for a in stored if a["id"] == "primary"), {}))
        sec = config.load_secrets(self.env_path)
        primary.update({k: sec[k] for k in SESSION_KEYS})
        profiles = [primary] + [a.copy() for a in stored if a["id"] != "primary"]
        for a in profiles:
            for k in SESSION_KEYS:
                a.setdefault(k, "")
            if a.get("tested_key") != profile_key(a):
                a.pop("egress_ip", None)
        return profiles

    def save(self, aid: str | None, label: str, session_values: dict | None = None,
             proxy: str | None = None, interval: float | None = None) -> str:
        label = label.strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("Название аккаунта: от 1 до 80 символов.")
        if interval is not None and (not math.isfinite(interval) or not 1 <= interval <= 600):
            raise ValueError("Пауза: от 1 до 600 секунд.")
        if proxy is not None:
            proxy = validate_proxy(proxy)
        with LOCK:
            profiles = self._read()
            if aid is None:
                if sum(a["id"] != "primary" for a in profiles) >= 199:
                    raise ValueError("Можно сохранить до 200 профилей.")
                aid = uuid.uuid4().hex[:16]
                a = {"id": aid, "enabled": False, **{k: "" for k in SESSION_KEYS}}
                profiles.append(a)
            else:
                a = next((a for a in profiles if a["id"] == aid), None)
                if a is None and aid == "primary":
                    a = {"id": aid, "enabled": True}
                    profiles.insert(0, a)
                if a is None:
                    raise ValueError("Аккаунт не найден.")
            changes = {k: v for k, v in (session_values or {}).items() if k in SESSION_KEYS}
            if proxy is not None:
                changes["BUFF_PROXY"] = proxy
            cookie = changes.get("BUFF_COOKIE")
            if cookie and any(p["id"] != aid and p["BUFF_COOKIE"] == cookie for p in self.list()):
                raise ValueError("Эта сессия уже используется другим профилем.")
            a.update(label=label, interval=interval)
            if changes:
                a.pop("egress_ip", None)
                a.pop("tested_key", None)
                if aid != "primary":
                    a["enabled"] = False
                    a.update(changes)
                else:
                    write_env(self.env_path, changes)
            self._write(profiles)
            return aid

    def tested(self, aid: str, expected_key: str, ip: str) -> None:
        ip = str(ipaddress.ip_address(ip))
        with LOCK:
            live = next((a for a in self.list() if a["id"] == aid), None)
            if not live or profile_key(live) != expected_key:
                raise ValueError("Настройки аккаунта изменились; повторите проверку.")
            profiles = self._read()
            a = next((a for a in profiles if a["id"] == aid), None)
            if a is None:
                a = {"id": aid, "label": live["label"], "enabled": live["enabled"], "interval": live["interval"]}
                profiles.insert(0, a)
            a.update(egress_ip=ip, tested_key=expected_key)
            self._write(profiles)

    def receive_session(self, aid: str, expected_key: str, values: dict, ip: str, steam_login_hash=None) -> None:
        """Commit a validated browser session only if the profile wasn't edited/deleted."""
        ip = str(ipaddress.ip_address(ip))
        changes = {k: values[k] for k in SESSION_KEYS if k != "BUFF_PROXY" and k in values}
        if not changes.get("BUFF_COOKIE"):
            raise ValueError("Браузер не передал сессию BuffMarket.")
        with LOCK:
            live = next((a for a in self.list() if a["id"] == aid), None)
            if not live or profile_key(live) != expected_key:
                raise ValueError("Настройки аккаунта изменились; начните вход заново.")
            if any(a["id"] != aid and a["BUFF_COOKIE"] == changes["BUFF_COOKIE"] for a in self.list()):
                raise ValueError("Эта сессия уже используется другим профилем.")
            if steam_login_hash and any(a["id"] != aid and a.get("steam_login_hash") == steam_login_hash for a in self.list()):
                raise ValueError("Этот Steam-логин уже привязан к другому профилю.")
            updated = dict(live, **changes)
            profiles = self._read()
            a = next((a for a in profiles if a["id"] == aid), None)
            if a is None:
                a = {k: live[k] for k in ("id", "label", "enabled", "interval")}
                profiles.insert(0, a)
            if aid == "primary":
                write_env(self.env_path, changes)
            else:
                a.update(changes)
            a.update(egress_ip=ip, tested_key=profile_key(updated))
            if steam_login_hash:
                a["steam_login_hash"] = steam_login_hash
            self._write(profiles)

    def import_bulk(self, rows: list[dict], interval: float | None) -> list[dict]:
        """Validate the entire import before one atomic write; passwords stay in RAM."""
        if interval is not None and (not math.isfinite(interval) or not 1 <= interval <= 600):
            raise ValueError("Пауза: от 1 до 600 секунд.")
        with LOCK:
            profiles = self._read()
            live_profiles = self.list()
            existing = {a.get("steam_login_hash"): a for a in live_profiles if a.get("steam_login_hash")}
            out, seen = [], set()
            for row in rows:
                key = login_key(row["username"])
                if key in seen:
                    raise ValueError(f"Строка {row['line']}: логин повторяется в списке.")
                seen.add(key)
                proxy = validate_proxy(row["proxy"])
                a = existing.get(key)
                if a is None:
                    # Older browser logins saved a hash in the private profile path.
                    for old in live_profiles:
                        marker = self.path.parent / "browser_profiles" / old["id"] / hashlib.sha256(old["BUFF_PROXY"].encode()).hexdigest()[:16] / "last_profile"
                        try:
                            old_hash = marker.read_text(encoding="ascii").strip()
                        except (OSError, UnicodeError):
                            continue
                        if old_hash == key[:32]:
                            a = old
                            stored = next((p for p in profiles if p["id"] == old["id"]), None)
                            if stored is None:
                                stored = {k: old[k] for k in ("id", "label", "enabled", "interval")}
                                profiles.append(stored)
                            stored["steam_login_hash"] = key
                            existing[key] = old
                            break
                if a:
                    if a["BUFF_PROXY"] != proxy:
                        raise ValueError(f"Строка {row['line']}: у существующего аккаунта другой прокси. Измените его отдельно.")
                    created = False
                else:
                    a = {"id": uuid.uuid4().hex[:16], "label": row["username"][:80], "interval": interval,
                         "enabled": False, "steam_login_hash": key, **{k: "" for k in SESSION_KEYS}}
                    a["BUFF_PROXY"] = proxy
                    profiles.append(a)
                    existing[key] = a
                    created = True
                out.append(dict(row, id=a["id"], label=a["label"], created=created))
            if sum(a["id"] != "primary" for a in profiles) > 199:
                raise ValueError("Недостаточно свободных мест: предел 200 профилей вместе с основным.")
            self._write(profiles)
            return out

    def enable_verified(self, aid: str, expected_key: str) -> None:
        with LOCK:
            a = next((a for a in self.list() if a["id"] == aid), None)
            if not a or profile_key(a) != expected_key or not a["BUFF_COOKIE"] or not a.get("egress_ip"):
                raise ValueError("Сессия или настройки изменились; повторите проверку.")
            profiles = self._read()
            target = next((p for p in profiles if p["id"] == aid), None)
            if target is None:
                target = {k: a[k] for k in ("id", "label", "interval")}
                profiles.append(target)
            target["enabled"] = True
            self._write(profiles)

    def toggle(self, aid: str) -> None:
        with LOCK:
            live = next((a for a in self.list() if a["id"] == aid), None)
            if not live:
                raise ValueError("Аккаунт не найден.")
            if not live["enabled"] and (not live["BUFF_COOKIE"] or not live.get("egress_ip")):
                raise ValueError("Сначала сохраните сессию и успешно проверьте аккаунт и IP.")
            profiles = self._read()
            a = next((a for a in profiles if a["id"] == aid), None)
            if a is None:
                a = {"id": aid, "label": live["label"], "interval": live["interval"]}
                profiles.insert(0, a)
            a["enabled"] = not live["enabled"]
            self._write(profiles)

    def remove(self, aid: str) -> None:
        if aid == "primary":
            raise ValueError("Основной аккаунт можно выключить, но нельзя удалить.")
        with LOCK:
            self._write([a for a in self._read() if a["id"] != aid])

    def set_enabled(self, ids: list[str], enabled: bool) -> int:
        with LOCK:
            live = {a["id"]: a for a in self.list()}
            selected = [live[i] for i in set(ids) if i in live]
            if enabled and any(not a["BUFF_COOKIE"] or not a.get("egress_ip") for a in selected):
                raise ValueError("Сначала проверьте сессию и IP всех выбранных аккаунтов.")
            profiles = self._read()
            changed = 0
            for a in selected:
                if a["enabled"] == enabled:
                    continue
                saved = next((p for p in profiles if p["id"] == a["id"]), None)
                if saved is None:
                    saved = {k: a[k] for k in ("id", "label", "interval")}
                    profiles.insert(0, saved)
                saved["enabled"] = enabled
                changed += 1
            if changed:
                self._write(profiles)
            return changed
