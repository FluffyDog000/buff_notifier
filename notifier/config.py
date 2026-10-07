"""Settings and secrets.

Two places, on purpose:

- `data/settings.json` - thresholds, fees, pacing: nothing secret, edited
  from the web page, re-read by the notifier every cycle.
- `.env` (mode 600) - the BuffMarket session, the proxy (its URL carries a
  password), the Telegram token, the web password hash. Never in the
  database, never in logs, shown on the page only as the last characters.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .envfile import read_env

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENV_PATH = ROOT / ".env"
SETTINGS_PATH = DATA / "settings.json"
STORE_PATH = DATA / "buff.db"

SECRET_KEYS = ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT", "BUFF_PROXY",
               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
               "WEB_PASSWORD_HASH", "WEB_SECRET_KEY")


@dataclass(frozen=True)
class Field:
    key: str
    kind: str            # float | pct | int | bool | str | choice
    default: object
    label: str
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    group: str = ""


FIELDS: tuple[Field, ...] = (
    Field("min_discount", "pct", 0.15, "Дешевле рынка на, %",
          "Цена лота с комиссией ниже выручки на CSFloat (медиана сотой минус "
          "комиссия CSFloat) хотя бы на столько.", 0, 0.9, group="Сигналы"),
    Field("price_basis", "choice", "min", "Чем оценивать рынок",
          "Меньшая из двух — и медиана предмета, и медиана сотой float лота: редкие "
          "дорогие продажи в узкой сотой не раздувают оценку, а лот с худшим float не "
          "кажется дешёвым. Медиана сотой — точнее для дорогих float, но шумит на малых выборках.",
          choices=(("min", "меньшая из двух: предмет и сотая float"),
                   ("item", "медиана предмета"),
                   ("bucket", "медиана сотой float")), group="Сигналы"),
    Field("min_profit_usd", "float", 1.0, "Выгода не меньше, $",
          "Отсекает копеечные лоты, где проценты большие, а денег нет.", 0, 10000,
          group="Сигналы"),
    Field("float_signal", "bool", True, "Сигнал «дорогой float по обычной цене»",
          group="Сигналы"),
    Field("min_float_premium", "pct", 0.10, "Сотая дороже соседней на, %",
          "Медиана сотой лота выше медианы соседней «обычной» сотой хотя бы на "
          "столько.", 0, 5, group="Сигналы"),
    Field("normal_tolerance", "pct", 0.03, "«Обычная цена» — не выше обычной сотой на, %",
          "Лот стоит не дороже медианы соседней сотой плюс столько.", 0, 1,
          group="Сигналы"),
    Field("allow_item_median", "bool", False, "Оценивать по всему предмету, если в сотой мало продаж",
          "Выключено: лот без 5 продаж в своей сотой не оценивается. Медиана "
          "предмета для дешёвой сотой завышает рынок.", group="Сигналы"),
    Field("pattern_skins", "choice", "skip", "Паттерновые скины",
          "Fade, Marble Fade, Case Hardened, Heat Treated, Crimson Web: цена от паттерна, "
          "медиана по float обманывает.", choices=(("skip", "не уведомлять"),
                                                  ("notify", "уведомлять с пометкой")),
          group="Сигналы"),
    Field("csfloat_fee", "pct", 0.02, "Комиссия CSFloat при продаже, %", "", 0, 0.5,
          group="Цены"),
    Field("buff_fee", "pct", 0.0, "Комиссия BuffMarket при покупке, %",
          "Сверху цены лота: пополнение, оплата.", 0, 0.5, group="Цены"),
    Field("usd_per_buff", "float", 1.0, "Курс: $ за единицу цены BuffMarket",
          "1, если цены на BuffMarket в долларах.", 0.0001, 1000, group="Цены"),
    Field("window_days", "float", 16.0, "Окно продаж CSFloat, дней",
          "Для редких предметов удлиняется до 30 и 45 дней, как в боте.", 3, 45,
          group="Цены"),
    Field("request_interval", "float", 5.0, "Пауза между запросами к BuffMarket, с",
          "Все запросы идут по одному. Ограничивает сайт — увеличьте.", 1, 600,
          group="Опрос"),
    Field("poll_scale", "float", 720.0, "Интервал опроса = N / продаж в сутки, мин",
          "Предмет с 48 продажами в сутки при N = 720 опрашивается раз в 15 минут.",
          1, 100000, group="Опрос"),
    Field("poll_min_minutes", "float", 5.0, "Опрос не чаще, чем раз в, мин", "", 1, 1440,
          group="Опрос"),
    Field("poll_max_minutes", "float", 180.0, "Опрос не реже, чем раз в, мин", "", 1, 10080,
          group="Опрос"),
    Field("page_size", "int", 10, "Лотов за запрос",
          "Сколько новейших лотов брать за один запрос.", 1, 50, group="Опрос"),
    Field("paused", "bool", False, "Пауза: не опрашивать BuffMarket", group="Опрос"),
    Field("csfloat_db", "str", "/root/csfloatpricesparcing/data/csfloat_sales.db",
          "База CSFloat-бота", "Открывается только на чтение. Если бот на другом сервере — "
          "сюда сама встанет копия data/csfloat_snapshot.db.", group="Опрос"),
    Field("csfloat_remote", "str", "", "Сервер CSFloat-бота для копии базы",
          "Если бот работает на другом сервере: root@его-IP. Раз в час оттуда приходит "
          "копия базы (docs/DEPLOY.md, раздел «База с другого сервера»). Пусто — база здесь.",
          group="Опрос"),
)
FIELD = {f.key: f for f in FIELDS}


def defaults() -> dict:
    return {f.key: f.default for f in FIELDS}


def _coerce(f: Field, raw):
    if f.kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "on", "yes", "да")
        return bool(raw)
    if f.kind == "str":
        return str(raw).strip()
    if f.kind == "choice":
        v = str(raw)
        if v not in [c[0] for c in f.choices]:
            raise ValueError("нет такого варианта")
        return v
    v = float(str(raw).replace(",", ".").strip())
    if f.kind == "int":
        v = int(round(v))
    if (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
        raise ValueError(f"допустимо от {f.lo:g} до {f.hi:g}")
    return v


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    out = defaults()
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    for k, v in stored.items():
        f = FIELD.get(k)
        if f is None:
            continue
        try:
            out[k] = _coerce(f, v)
        except (TypeError, ValueError):
            pass
    # The database path may also come from the old .env line.
    if "csfloat_db" not in stored:
        env = os.environ.get("CSFLOAT_DB_PATH") or read_env(ENV_PATH).get("CSFLOAT_DB_PATH")
        if env:
            out["csfloat_db"] = env
    return out


def parse_form(form) -> tuple[dict, dict]:
    """Values from a web form (percent fields typed as percents) and errors
    by key. A bool missing from the form is an unticked box."""
    values, errors = {}, {}
    for f in FIELDS:
        raw = form.get(f.key)
        if f.kind == "bool":
            values[f.key] = raw is not None and raw not in ("", "0", "off")
            continue
        if raw is None:
            continue
        try:
            if f.kind == "pct":
                raw = float(str(raw).replace(",", ".").strip()) / 100.0
            values[f.key] = _coerce(f, raw)
        except (TypeError, ValueError) as e:
            errors[f.key] = str(e) if str(e) and "could not" not in str(e) else "не число"
    if not errors and values.get("poll_min_minutes", 0) > values.get("poll_max_minutes", 1e9):
        errors["poll_min_minutes"] = "больше, чем «не реже»"
    return values, errors


def save_settings(values: dict, path: Path = SETTINGS_PATH) -> dict:
    current = load_settings(path)
    current.update({k: v for k, v in values.items() if k in FIELD})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return current


def load_secrets(path: Path = ENV_PATH) -> dict[str, str]:
    env = read_env(path)
    return {k: (env.get(k) or os.environ.get(k) or "").strip() for k in SECRET_KEYS}


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    return "…" + value[-keep:] if len(value) > keep * 2 else "задано"
