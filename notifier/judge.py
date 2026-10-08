"""Is a BuffMarket listing worth an alert?

The market is the CSFloat bot's sales history: the item's sales over a window
long enough for its liquidity (16 days, or 30 / 45 for thin items, as the bot
reads them), each brought to today's price level, then the median of the
listing's own hundredth of float (`estimate`).

    cost   = price on BuffMarket x rate x (1 + BuffMarket fee)
    net    = median of the hundredth x (1 - CSFloat fee)
    profit = net - cost

Two signals:

1. cheap - cost is below the chosen median or its net proceeds by
   `min_discount` or more. The reference can be the item, the float bucket,
   their minimum (both conditions), or maximum (either condition). Both
   minimum profit filters also apply. Unpainted knives use the item median.
2. float - the listing's hundredth sells for `min_float_premium` more than
   a neighbouring hundredth, while the listing is priced like that ordinary
   neighbour (no more than `normal_tolerance` above its median) and still
   leaves `min_profit_usd`.

Pattern skins (Fade, Marble Fade, Case Hardened, Heat Treated, Crimson Web) are priced by
the seed, not the float: skipped, or alerted with a warning.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from . import phases
from .estimate import BUCKET_MIN_SALES, ITEM_MIN_SALES, estimate
from .listings import Listing
from .localtime import moscow
from .names import vanilla
from .recency import choose_window, to_today, within

# Read 45 days back: the longest window `choose_window` may settle on.
HISTORY_DAYS = 45.0
_PATTERN = re.compile(r"\| (Fade|Marble Fade|Case Hardened|Heat Treated|Crimson Web) \(")


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
        if sales and all(r.get("float_value") is None for r in sales):
            window = next((w for w in sorted({window_days, 30., 45.}) if w >= window_days
                           and len(within(sales, w)) >= ITEM_MIN_SALES), 45.)
        rows, info = to_today(within(sales, window), window)
        rows = [r for r in rows if r.get("price")]
        m = cls(rows=rows, window=window, shift=info.shift if info.applied else None)
        for r in rows:
            if r.get("float_value") is not None:
                m.buckets.setdefault(_bucket(float(r["float_value"])), []).append(float(r["price"]))
        return m

    def item_median(self) -> tuple[float | None, int]:
        """The whole item's median: what a typical float sells for."""
        if len(self.rows) < ITEM_MIN_SALES:
            return None, len(self.rows)
        return statistics.median(float(r["price"]) for r in self.rows), len(self.rows)

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
    reference: str = ""                # brief explanation for the notification
    discount: float | None = None


def judge(listing: Listing, name: str, market: Market, s: dict) -> Verdict:
    if not listing.listed:
        return Verdict(None, "лот снят")
    unpainted = vanilla(name)
    if listing.float_value is None and not unpainted:
        return Verdict(None, "нет float")
    _, phase_index = phases.split(name)
    if phase_index is not None and listing.paint_index != phase_index:
        return Verdict(None, "другая фаза")
    pattern = is_pattern_skin(name)
    if pattern and s["pattern_skins"] == "skip":
        return Verdict(None, "паттерновый скин")

    cost = listing.price * s["usd_per_buff"] * (1 + s["buff_fee"])
    lo = _bucket(listing.float_value) if listing.float_value is not None else None
    item_med, n_all = market.item_median()
    b_med, n_b = market.bucket_median(lo) if lo is not None and not unpainted else (None, 0)
    band = f"{lo:.2f}–{lo + 0.01:.2f}" if lo is not None else "—"
    mode = s.get("price_basis", "min")

    if unpainted:
        expected, basis = item_med, f"медиана {n_all} продаж ванильного ножа"
    elif mode == "either":
        choices = [(med, title) for med, title in ((b_med, f"float {band}"), (item_med, "весь предмет")) if med is not None]
        expected, reference = max(choices, default=(None, ""), key=lambda x: x[0])
        basis = f"медиана {reference} (любое из двух условий)"
    elif mode == "bucket":
        expected, basis = estimate(market.rows, listing.float_value)
        if b_med is None and not s["allow_item_median"]:
            expected = None
    elif mode == "item":
        expected, basis = item_med, f"медиана {n_all} продаж предмета"
    elif b_med is not None and item_med is not None:
        expected = min(b_med, item_med)
        basis = (f"меньшая из медиан: сотая {band} — ${b_med:.2f} ({n_b} продаж), "
                 f"предмет — ${item_med:.2f} ({n_all} продаж)")
    elif s["allow_item_median"]:
        expected, basis = item_med, f"медиана {n_all} продаж предмета (в сотой {band} мало продаж)"
    else:
        expected, basis = None, ""
    if expected is None:
        return Verdict(None, f"мало продаж в сотой {band} ({n_b})", cost=cost)
    days = f" за {market.window:g} дн."
    if market.shift is not None and abs(market.shift) >= 0.005:
        days += f", цены приведены к сегодняшним ({market.shift:+.0%} за окно)"

    net = expected * (1 - s["csfloat_fee"])
    profit = net - cost
    reference = ("ванильный нож" if unpainted else reference if mode == "either" else
                 "весь предмет" if mode == "item" or b_med is None else
                 f"float {band}" if mode == "bucket" or b_med <= item_med else "весь предмет")
    comparison = expected if s.get("discount_basis", "net") == "median" else net
    discount = 1 - cost / comparison if comparison > 0 else 0
    v = Verdict(None, cost=cost, expected=expected, net=net, profit=profit,
                profit_pct=profit / cost if cost else None, basis=basis + days, pattern=pattern,
                reference=f"{reference} · {market.window:g} дн.", discount=discount)
    if (profit >= s["min_profit_usd"] and v.profit_pct >= s.get("min_profit_pct", 0)
            and comparison > 0 and discount + 1e-12 >= s["min_discount"]):
        v.kind = "cheap"
        return v

    # A dear hundredth sold at an ordinary price: priced by the hundredth
    # itself, against its cheaper neighbour - that gap is the whole signal.
    if s["float_signal"] and b_med is not None:
        best = None
        for nlo in (round(lo - 0.01, 2), round(lo + 0.01, 2)):
            med, _ = market.bucket_median(nlo)
            if med and (best is None or med < best[0]):
                best = (med, nlo)
        if best:
            normal, nlo = best
            premium = b_med / normal - 1
            b_net = b_med * (1 - s["csfloat_fee"])
            if (premium >= s["min_float_premium"] and cost <= normal * (1 + s["normal_tolerance"])
                    and b_net - cost >= s["min_profit_usd"]):
                if (b_net - cost) / cost < s.get("min_profit_pct", 0):
                    v.reason = "выгода ниже порога в процентах"
                    return v
                return Verdict("float", cost=cost, expected=b_med, net=b_net, profit=b_net - cost,
                               profit_pct=(b_net - cost) / cost, pattern=pattern,
                               basis=f"медиана {n_b} продаж в {band}" + days,
                               normal=normal, normal_lo=nlo, premium=premium,
                               reference=f"float {band} · {market.window:g} дн.")
    v.reason = (f"выгода ${profit:.2f}" if profit < s["min_profit_usd"] else
                "выгода ниже порога в процентах" if v.profit_pct < s.get("min_profit_pct", 0) else
                f"скидка {discount:.0%}" if comparison > 0 else "нет оценки")
    return v


def message(listing: Listing, name: str, v: Verdict, link: str) -> str:
    head = "🟢 Дешевле рынка" if v.kind == "cheap" else "💎 Выгодный float"
    lines = [head, name]
    if v.pattern:
        lines.append(f"⚠️ Паттерновый скин · seed {listing.paint_seed}")
    lines.append(f"💵 Buff: ${listing.price:.2f}" + (f" · скидка {v.discount:.0%}" if v.discount is not None else "")
                 + (f" · затраты ${v.cost:.2f}" if abs(v.cost - listing.price) > .005 else ""))
    lines.append(f"📊 CSFloat: ${v.expected:.2f} · {v.reference}")
    lines.append(f"💰 Расчётная выгода: +${v.profit:.2f} ({v.profit_pct:+.1%})")
    if listing.float_value is not None and not vanilla(name):
        lines.append(f"🔬 Float {listing.float_value:.5f}")
    if listing.created_at:
        lines.append(f"🕒 {moscow(listing.created_at):%d.%m %H:%M} МСК")
    lines.append(f"🔗 {link}")
    return "\n".join(lines)
