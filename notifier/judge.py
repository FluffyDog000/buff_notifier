"""Price listings from comparable sales, with a conservative uncertainty reserve.

At least five close-float sales are required, within the same phase and
pattern seed where applicable. The whole-item price may cap an estimate,
but never substitutes for missing comparable sales. Stickers are not valued.
"""
from __future__ import annotations

import re
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from . import phases
from .estimate import BUCKET_MIN_SALES
from .listings import Listing
from .localtime import moscow
from .names import vanilla
from .recency import careful_median, to_today, within

# Read 45 days back: the longest comparable-sales window.
HISTORY_DAYS = 45.0
_PATTERN = re.compile(r"\| (Fade|Marble Fade|Case Hardened|Heat Treated|Crimson Web) \(")


def is_pattern_skin(name: str) -> bool:
    return bool(_PATTERN.search(phases.split(name)[0]))


def _bucket(f: float) -> float:
    """The hundredth a float falls in, cut exactly as `estimate` cuts it."""
    return int(f * 100) / 100.0


@dataclass
class Market:
    """Raw item history, with lazily prepared phase/seed/window models."""
    rows: list[dict]
    window: float
    shift: float | None = None          # level today vs window start, -0.03 = 3% down
    base_window: float = 16.
    prepared: dict = field(default_factory=dict, repr=False)
    groups: dict = field(default_factory=dict, repr=False)
    float_index: dict | None = field(default=None, repr=False)
    whole_price: float | None = field(default=None, repr=False)

    @classmethod
    def build(cls, sales: list[dict], window_days: float) -> "Market":
        return cls(rows=sales, window=window_days, base_window=window_days)

    def close_rows(self, f: float | None) -> list[dict]:
        if f is None:
            return self.rows
        if self.float_index is None:
            buckets = {}
            for row in self.rows:
                if row.get("float_value") is not None:
                    buckets.setdefault(_bucket(float(row["float_value"])), []).append(row)
            self.float_index = {}
            for bucket, rows in buckets.items():
                rows.sort(key=lambda r: r["float_value"])
                self.float_index[bucket] = ([float(r["float_value"]) for r in rows], rows)
        # Do not mix near-perfect floats with the far end of their hundredth.
        radius = .001 if f < .01 or f >= .99 else .005
        values, rows = self.float_index.get(_bucket(f), ([], []))
        return rows[bisect_left(values, f - radius - 1e-12):bisect_right(values, f + radius + 1e-12)]

    def item_median(self) -> float:
        if self.whole_price is None:
            self.whole_price = statistics.median(r["price"] for r in self.rows)
        return self.whole_price

    def comparable(self, listing: Listing, phase: int | None,
                   pattern: bool, unpainted: bool, min_recent: int = 0,
                   recent_days: float = 7.) -> tuple["Market", list[dict]]:
        group_key = (phase, listing.paint_seed if pattern else None)
        if group_key not in self.groups:
            eligible = [r for r in self.rows if (r.get("price") or 0) > 0
                        and (phase is None or r.get("paint_index") == phase)
                        and (not pattern or r.get("paint_seed") == listing.paint_seed)]
            self.groups[group_key] = Market(eligible, self.base_window, base_window=self.base_window)
        group = self.groups[group_key]
        f = None if unpainted else listing.float_value
        matching = group.close_rows(f)
        windows = sorted({self.base_window, 30., 45.})
        windows = [w for w in windows if w >= self.base_window]
        window = next((w for w in windows if len(within(matching, w))
                       >= BUCKET_MIN_SALES), windows[-1])
        selected = within(matching, window)
        recent = sum(1 for r in selected if r.get("age_days") is not None
                     and 0 <= r["age_days"] <= recent_days)
        if len(selected) < BUCKET_MIN_SALES or recent < min_recent:
            # No trend can repair missing observations. Reject before the
            # quadratic fit, retaining the same window/count for diagnostics.
            return Market(selected, window, base_window=window), selected
        # Trend fitting is shared across listings with the same phase/seed/window.
        key = (phase, listing.paint_seed if pattern else None, window)
        if key not in self.prepared:
            rows, info = to_today(within(group.rows, window), window, max_points=96)
            self.prepared[key] = Market(rows, window, info.shift if info.applied else None,
                                        base_window=window)
        model = self.prepared[key]
        return model, model.close_rows(f)


@dataclass(frozen=True)
class PriceSample:
    expected: float
    median: float
    count: int
    confidence: str
    recent_count: int
    recent_cap: float
    recent_median: float

    @classmethod
    def build(cls, rows: list[dict], min_recent: int = 5, recent_days: float = 7.,
              percentile: float = .25) -> "PriceSample | None":
        if len(rows) < BUCKET_MIN_SALES:
            return None
        prices = [float(r["price"]) for r in rows]
        median = statistics.median(prices)
        mad = statistics.median(abs(p - median) for p in prices)
        fresh = [r for r in rows if r.get("age_days") is not None and 0 <= r["age_days"] <= recent_days]
        recent = len(fresh)
        if recent < min_recent:
            return None
        # A quality label, not a calibrated probability of a future sale.
        confidence = ("низкая" if len(rows) < 10 or recent < 3 or mad / median > .15
                      else "высокая" if len(rows) >= 30 and recent >= 10 and mad / median <= .1
                      else "средняя")
        expected, _ = careful_median(prices, k=1.5 if confidence == "низкая" else 1.)
        fresh_prices = sorted(min(float(r["price"]), float(r.get("raw_price") or r["price"])) for r in fresh)
        position = (len(fresh_prices) - 1) * percentile
        lo = int(position)
        hi = min(lo + 1, len(fresh_prices) - 1)
        cap = fresh_prices[lo] + (fresh_prices[hi] - fresh_prices[lo]) * (position - lo)
        return cls(max(0., min(expected, cap)), median, len(rows), confidence, recent, cap,
                   statistics.median(fresh_prices))


@dataclass
class Verdict:
    kind: str | None                    # cheap | float | None
    reason: str = ""                    # why not, when kind is None
    cost: float | None = None
    expected: float | None = None       # conservative CSFloat sale-price estimate
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
    sample_count: int = 0
    confidence: str = ""
    raw_median: float | None = None
    recent_count: int = 0


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
    if "Doppler" in name:
        phase_index = phase_index or listing.paint_index
        if phase_index is None:
            return Verdict(None, "недостаточно данных: неизвестна фаза")
    if pattern and listing.paint_seed is None:
        return Verdict(None, "недостаточно данных: неизвестен seed")

    cost = listing.price * s["usd_per_buff"] * (1 + s["buff_fee"])
    lo = _bucket(listing.float_value) if listing.float_value is not None else None
    min_recent = s.get("min_recent_sales", 5)
    recent_days = s.get("recent_sales_days", 7.)
    percentile = s.get("recent_price_percentile", .25)
    market, comparable = market.comparable(listing, phase_index, pattern, unpainted,
                                          min_recent, recent_days)
    sample = PriceSample.build(comparable, min_recent, recent_days, percentile)
    band = f"{lo:.2f}–{lo + 0.01:.2f}" if lo is not None else "—"
    mode = s.get("price_basis", "min")

    if sample is None:
        fresh = sum(1 for r in comparable if r.get("age_days") is not None and 0 <= r["age_days"] <= recent_days)
        reason = (f"мало продаж похожих лотов ({len(comparable)}/5)" if len(comparable) < BUCKET_MIN_SALES
                  else f"свежих похожих продаж {fresh}/{min_recent} за {recent_days:g} дн.")
        return Verdict(None, f"недостаточно данных: {reason} "
                       f"в {band} за {market.window:g} дн.", cost=cost,
                       sample_count=len(comparable))
    b_med = None if unpainted else sample.expected
    expected = sample.expected
    reference = "ванильный нож" if unpainted else f"float {band}"
    basis = (f"{reference}: {sample.count} похожих продаж, медиана ${sample.median:.2f}, "
             f"оценка с резервом ${sample.expected:.2f}; предел свежих ${sample.recent_cap:.2f} "
             f"(P{percentile * 100:g}, {sample.recent_count} за {recent_days:g} дн.); надёжность {sample.confidence}")
    if phase_index is not None:
        basis += f"; paint index {phase_index}"
    if pattern:
        basis += f"; seed {listing.paint_seed}"
    float_basis = basis
    # Legacy 'item' and 'either' settings also require comparable sales.
    # A whole-item estimate is only an additional conservative cap.
    if not unpainted and mode in ("min", "item"):
        whole = market.item_median()
        if whole < expected:
            expected = whole
            basis += f"; ограничено ценой предмета ${expected:.2f}"
    if expected <= 0:
        return Verdict(None, "недостаточно данных: слишком большой разброс цен", cost=cost)
    days = f" за {market.window:g} дн."
    if market.shift is not None and abs(market.shift) >= 0.005:
        days += f", цены приведены к сегодняшним ({market.shift:+.0%} за окно)"

    net = expected * (1 - s["csfloat_fee"])
    profit = net - cost
    comparison = expected if s.get("discount_basis", "net") == "median" else net
    discount = 1 - cost / comparison if comparison > 0 else 0
    v = Verdict(None, cost=cost, expected=expected, net=net, profit=profit,
                profit_pct=profit / cost if cost else None, basis=basis + days, pattern=pattern,
                reference=f"{reference} · {market.window:g} дн.", discount=discount,
                sample_count=sample.count, confidence=sample.confidence, raw_median=sample.median,
                recent_count=sample.recent_count)
    if (s.get("cheap_signal", True) and profit >= s["min_profit_usd"] and v.profit_pct >= s.get("min_profit_pct", 0)
            and comparison > 0 and discount + 1e-12 >= s["min_discount"]):
        v.kind = "cheap"
        return v

    # A dear hundredth sold at an ordinary price: priced by the hundredth
    # itself, against its cheaper neighbour - that gap is the whole signal.
    if s["float_signal"] and b_med is not None:
        best = None
        for nlo in (round(lo - 0.01, 2), round(lo + 0.01, 2)):
            neighbour = market.close_rows(round(listing.float_value + nlo - lo, 10))
            neighbour_sample = PriceSample.build(neighbour, min_recent, recent_days, percentile)
            med = neighbour_sample.recent_median if neighbour_sample is not None else None
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
                               basis=float_basis + days + f"; свежая медиана соседней сотой {nlo:.2f} — ${normal:.2f}",
                               normal=normal, normal_lo=nlo, premium=premium,
                               reference=f"float {band} · {market.window:g} дн.",
                               sample_count=sample.count, confidence=sample.confidence, raw_median=sample.median,
                               recent_count=sample.recent_count)
    v.reason = (f"выгода ${profit:.2f}" if profit < s["min_profit_usd"] else
                "выгода ниже порога в процентах" if v.profit_pct < s.get("min_profit_pct", 0) else
                "сигнал дешевле рынка выключен" if not s.get("cheap_signal", True) else
                f"скидка {discount:.0%}" if comparison > 0 else "нет оценки")
    return v


def message(listing: Listing, name: str, v: Verdict, link: str) -> str:
    head = "🟢 Дешевле рынка" if v.kind == "cheap" else "💎 Выгодный float"
    lines = [head, name]
    if v.pattern:
        lines.append(f"⚠️ Паттерновый скин · seed {listing.paint_seed}")
    lines.append(f"💵 Buff: ${listing.price:.2f}" + (f" · скидка {v.discount:.0%}" if v.discount is not None else "")
                 + (f" · затраты ${v.cost:.2f}" if abs(v.cost - listing.price) > .005 else ""))
    lines.append(f"📊 CSFloat: ≈${v.expected:.2f} · {v.reference} · {v.sample_count} продаж, {v.recent_count} свежих · {v.confidence}")
    lines.append(f"💰 Расчётная выгода: +${v.profit:.2f} ({v.profit_pct:+.1%})")
    if listing.float_value is not None and not vanilla(name):
        lines.append(f"🔬 Float {listing.float_value:.5f}")
    if listing.created_at:
        lines.append(f"🕒 {moscow(listing.created_at):%d.%m %H:%M} МСК")
    lines.append(f"🔗 {link}")
    return "\n".join(lines)
