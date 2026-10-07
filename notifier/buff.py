"""BuffMarket: the request the item page makes for its list of listings.

There is no official API. The page asks

    GET https://api.buff.market/api/market/goods/sell_order
        ?game=csgo&page_num=1&page_size=10&goods_id=5777&sort_by=created.desc

and that is all we ask too: one address, one request at a time, a pause
between them. No cookies and no CSRF token are sent - a GET does not need
them; if the site starts to, the answer is a shorter list or a slower pace,
not more accounts.

How the answer is read waits for a real response (`tools/probe.py`).
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
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class BuffClient:
    def __init__(self, min_interval: float = 5.0, timeout: float = 20.0,
                 session: requests.Session | None = None, clock=time.monotonic,
                 sleep=time.sleep):
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def _wait(self) -> None:
        if self._last is not None:
            left = self._last + self.min_interval - self._clock()
            if left > 0:
                self._sleep(left)
        self._last = self._clock()

    def sell_orders(self, goods_id: int, page_size: int = 10, page_num: int = 1,
                    sort_by: str = "created.desc") -> requests.Response:
        """The newest listings of one item, as the item page asks for them."""
        params = {"game": "csgo", "page_num": page_num, "page_size": page_size,
                  "goods_id": goods_id, "sort_by": sort_by}
        with self._lock:
            self._wait()
            r = self.session.get(API + SELL_ORDER, params=params, timeout=self.timeout)
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            raise BuffError("BuffMarket: 429, слишком часто", 429,
                            float(ra) if ra and ra.isdigit() else None)
        if r.status_code != 200:
            raise BuffError(f"BuffMarket: HTTP {r.status_code}", r.status_code)
        return r
