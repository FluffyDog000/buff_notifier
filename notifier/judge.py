"""Is a BuffMarket listing worth an alert?

The market is the CSFloat bot's sales history: the item's sales over a window
long enough for its liquidity (16 days, or 30 / 45 for thin items, as the bot
reads them), each brought to today's price level, then the median of the
listing's own hundredth of float (`estimate`).

    cost   = price on BuffMarket x rate x (1 + BuffMarket fee)
    net    = median of the hundredth x (1 - CSFloat fee)
    profit = net - cost

Two signals:

1. cheap - cost is below `net` by `min_discount` or more (and the profit
   is at least `min_profit_usd`).
2. float - the listing's hundredth sells for `min_float_premium` more than
   a neighbouring hundredth, while the listing is priced like that ordinary
   neighbour (no more than `normal_tolerance` above its median) and still
   leaves `min_profit_usd`.

Pattern skins (Fade, Marble Fade, Case Hardened, Crimson Web) are priced by
the seed, not the float: skipped, or alerted with a warning.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from . import phases
from .estimate import BUCKET_MIN_SALES, estimate
from .listings import Listing
from .recency import choose_window, to_today, within

# Read 45 days back: the longest window `choose_window` may settle on.
HISTORY_DAYS = 45.0
_PATTERN = re.compile(r"\| (Fade|Marble Fade|Case Hardened|Crimson Web) \(")


def is_pattern_skin(name: str) -> bool:
    return bool(_PATTERN.search(phases.split(name)[0]))


def _bucket(f: float) -> float:
    """The hundredth a float falls in, cut exactly as `estimate` cuts it."""
    return int(f * 100) / 100.0


@dataclass
class Market:
    """An item's sales, windowed and brought to today, ready to price floats."""
    rows: list[dict]
    window: float
    shift: float | None = None          # level today vs window start, -0.03 = 3% down
    buckets: dict[float, list[float]] = field(default_factory=dict)

    @classmethod
    def build(cls, sales: list[dict], window_days: float) -> "Market":
        window, _ = choose_window(sales, window_days, BUCKET_MIN_SALES, None)
        rows, info = to_today(within(sales, window), window)
        rows = [r for r in rows if r.get("price") and r.get("float_value") is not None]
        m = cls(rows=rows, window=window, shift=info.shift if info.applied else None)
        for r in rows:
            m.buckets.setdefault(_bucket(float(r["float_value"])), []).append(float(r["price"]))
        return m

    def bucket_median(self, lo: float) -> tuple[float | None, int]:
        prices = self.buckets.get(round(lo, 2), [])
        if len(prices) < BUCKET_MIN_SALES:
            return None, len(prices)
        return statistics.median(prices), len(prices)


@dataclass
class Verdict:
    kind: str | None                    # cheap | float | None
    reason: str = ""                    # why not, when kind is None
    cost: float | None = None
    expected: float | None = None       # CSFloat median the listing is priced by
    net: float | None = None
    profit: float | None = None
    profit_pct: float | None = None
    basis: str = ""
    normal: float | None = None         # the ordinary neighbour's median (float signal)
    normal_lo: float | None = None
    premium: float | None = None
    pattern: bool = False


def judge(listing: Listing, name: str, market: Market, s: dict) -> Verdict:
    if not listing.listed:
        return Verdict(None, "лот снят")
    if listing.float_value is None:
        return Verdict(None, "нет float")
    _, phase_index = phases.split(name)
    if phase_index is not None and listing.paint_index != phase_index:
        return Verdict(None, "другая фаза")
    pattern = is_pattern_skin(name)
    if pattern and s["pattern_skins"] == "skip":
        return Verdict(None, "паттерновый скин")

    cost = listing.price * s["usd_per_buff"] * (1 + s["buff_fee"])
    lo = _bucket(listing.float_value)
    expected, basis = estimate(market.rows, listing.float_value)
    in_bucket = market.bucket_median(lo)[1] >= BUCKET_MIN_SALES
    if expected is None or (not in_bucket and not s["allow_item_median"]):
        n = market.bucket_median(lo)[1]
        return Verdict(None, f"мало продаж в сотой {lo:.2f} ({n})", cost=cost)
    days = f" за {market.window:g} дн."
    basis += days
    if market.shift is not None and abs(market.shift) >= 0.005:
        basis += f", цены приведены к сегодняшним ({market.shift:+.0%} за окно)"

    net = expected * (1 - s["csfloat_fee"])
    profit = net - cost
    v = Verdict(None, cost=cost, expected=expected, net=net, profit=profit,
                profit_pct=profit / cost if cost else None, basis=basis, pattern=pattern)
    if profit < s["min_profit_usd"]:
        v.reason = f"выгода ${profit:.2f}"
        return v
    if net > 0 and 1 - cost / net >= s["min_discount"]:
        v.kind = "cheap"
        return v

    if s["float_signal"] and in_bucket:
        best = None
        for nlo in (round(lo - 0.01, 2), round(lo + 0.01, 2)):
            med, _ = market.bucket_median(nlo)
            if med and (best is None or med < best[0]):
                best = (med, nlo)
        if best:
            normal, nlo = best
            premium = expected / normal - 1
            v.normal, v.normal_lo, v.premium = normal, nlo, premium
            if premium >= s["min_float_premium"] and cost <= normal * (1 + s["normal_tolerance"]):
                v.kind = "float"
                return v
    v.reason = f"скидка {1 - cost / net:.0%}" if net > 0 else "нет оценки"
    return v


def message(listing: Listing, name: str, v: Verdict, link: str) -> str:
    head = "🟢 Дешевле рынка" if v.kind == "cheap" else "💎 Дорогой float по обычной цене"
    lines = [head, name]
    if v.pattern:
        lines.append("⚠️ Паттерновый скин: цена зависит от паттерна, оценка по float грубая")
    extra = f", наклейки: {', '.join(listing.stickers)}" if listing.stickers else ""
    lines.append(f"Float {listing.float_value:.5f}, паттерн {listing.paint_seed}{extra}")
    bargain = f" (торг от ${listing.bargain_floor:.2f})" if listing.bargain_floor else ""
    lines.append(f"Цена на BuffMarket: ${listing.price:.2f}{bargain}"
                 + (f", с комиссией ${v.cost:.2f}" if abs(v.cost - listing.price) > 0.005 else ""))
    lines.append(f"Ожидаемо на CSFloat: ${v.expected:.2f}, после комиссии ${v.net:.2f}")
    lines.append(f"Выгода: ${v.profit:.2f} ({v.profit_pct:.0%})")
    lines.append(f"Оценка: {v.basis}")
    if v.kind == "float" and v.normal is not None:
        lines.append(f"Сотая {_bucket(listing.float_value):.2f} дороже соседней "
                     f"{v.normal_lo:.2f} (${v.normal:.2f}) на {v.premium:.0%}")
    if listing.created_at:
        lines.append(f"Выставлен {listing.created_at:%d.%m %H:%M} UTC")
    lines.append(link)
    lines.append(f"Лот {listing.id}")
    return "\n".join(lines)
