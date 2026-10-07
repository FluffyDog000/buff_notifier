"""BuffMarket: the request the item page makes for its list of listings.

There is no official API. The page asks

    GET https://api.buff.market/api/market/goods/sell_order
        ?game=csgo&page_num=1&page_size=10&goods_id=5777&sort_by=created.desc

and that is all we ask too: one address, one request at a time, a pause
between them. Without a login it answers `{"code": "Login Required"}`, so
the one account's browser session goes along: its Cookie header, the CSRF
token and the User-Agent it was made with. If the site starts to limit us,
the answer is a shorter list or a slower pace, not more accounts.

One proxy may stand in for the server's own address - one fixed address
still, never a rotating pool: the point is a steady, ordinary visitor.
How the answer is read is `listings.py`.
"""
from __future__ import annotations

import threading
import time

import requests

API = "https://api.buff.market"
SELL_ORDER = "/api/market/goods/sell_order"
# The page's own headers, minus anything tied to a session.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Referer": "https://buff.market/",
    "Origin": "https://buff.market",
}


class BuffError(RuntimeError):
    def __init__(self, message: str, status: int | None = None,
                 retry_after: float | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.code = code


class LoginRequired(BuffError):
    """The session is missing or has expired: a new Cookie is needed."""


class BuffClient:
    def __init__(self, min_interval: float = 5.0, timeout: float = 20.0,
                 cookie: str = "", csrf: str = "", user_agent: str = "",
                 proxy: str = "", session: requests.Session | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        if cookie:
            self.session.headers["Cookie"] = cookie
        if csrf:
            self.session.headers["X-CSRFToken"] = csrf
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None
        self.last: requests.Response | None = None   # the latest raw answer
        self._lock = threading.Lock()

    def _wait(self) -> None:
        if self._last is not None:
            left = self._last + self.min_interval - self._clock()
            if left > 0:
                self._sleep(left)
        self._last = self._clock()

    def sell_orders(self, goods_id: int, page_size: int = 10, page_num: int = 1,
                    sort_by: str = "created.desc") -> dict:
        """The newest listings of one item, as the item page asks for them:
        the answer's JSON, once its `code` says it is one."""
        params = {"game": "csgo", "page_num": page_num, "page_size": page_size,
                  "goods_id": goods_id, "sort_by": sort_by}
        with self._lock:
            self._wait()
            r = self.session.get(API + SELL_ORDER, params=params, timeout=self.timeout)
            self.last = r
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            raise BuffError("BuffMarket: 429, слишком часто", 429,
                            float(ra) if ra and ra.isdigit() else None)
        if r.status_code != 200:
            raise BuffError(f"BuffMarket: HTTP {r.status_code}", r.status_code)
        try:
            body = r.json()
        except ValueError:
            raise BuffError(f"BuffMarket: не JSON: {r.text[:200]!r}", r.status_code) from None
        code = body.get("code") if isinstance(body, dict) else None
        if code == "Login Required":
            raise LoginRequired("BuffMarket: нужен вход - BUFF_COOKIE пуст или сессия истекла",
                                r.status_code, code=code)
        if code not in (None, "OK"):
            raise BuffError(f"BuffMarket: {code}: {body.get('error')}", r.status_code, code=code)
        return body


def from_config(settings: dict, secrets: dict, **kw) -> BuffClient:
    return BuffClient(settings["request_interval"], 20.0, secrets.get("BUFF_COOKIE", ""),
                      secrets.get("BUFF_CSRF", ""), secrets.get("BUFF_USER_AGENT", ""),
                      secrets.get("BUFF_PROXY", ""), **kw)


def fingerprint(settings: dict, secrets: dict) -> tuple:
    """What a client is built from: a change means a new client."""
    return (settings["request_interval"],) + tuple(
        secrets.get(k, "") for k in ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT", "BUFF_PROXY"))


def goods_name(body: dict, goods_id: int) -> str | None:
    """The item's market_hash_name, from the `goods_infos` every page carries."""
    infos = (body.get("data") or {}).get("goods_infos") or {}
    info = infos.get(str(goods_id)) or infos.get(goods_id) or {}
    return info.get("market_hash_name") or info.get("name")


def proxy_display(url: str) -> str:
    """scheme://host:port, without the login and password."""
    if not url:
        return ""
    scheme, _, rest = url.partition("://")
    host = rest.rsplit("@", 1)[-1]
    return f"{scheme}://{host}" if rest else url.rsplit("@", 1)[-1]
