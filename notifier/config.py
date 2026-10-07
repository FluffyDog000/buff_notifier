"""Settings from the environment (and `.env` beside the code, if present).

Secrets, when there are any, live only in `.env` with mode 600 - never in
code, in the database or in logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv is a convenience, not a requirement
    pass


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # The CSFloat bot's database, opened read-only.
    csfloat_db: str = "/root/csfloatpricesparcing/data/csfloat_sales.db"
    # Seconds between two requests to BuffMarket, whatever asks for them.
    buff_min_interval: float = 5.0
    buff_timeout: float = 20.0
    # The one account's browser session: the whole Cookie header, the CSRF
    # token the page sends, and the User-Agent of the browser they came from
    # (a session may be tied to it). Secrets - `.env` only, mode 600.
    buff_cookie: str = ""
    buff_csrf: str = ""
    buff_user_agent: str = ""


def load() -> Config:
    d = Config()
    return Config(
        csfloat_db=os.environ.get("CSFLOAT_DB_PATH", "").strip() or d.csfloat_db,
        buff_min_interval=_f("BUFF_MIN_INTERVAL", d.buff_min_interval),
        buff_timeout=_f("BUFF_TIMEOUT", d.buff_timeout),
        buff_cookie=os.environ.get("BUFF_COOKIE", "").strip(),
        buff_csrf=os.environ.get("BUFF_CSRF", "").strip(),
        buff_user_agent=os.environ.get("BUFF_USER_AGENT", "").strip(),
    )
