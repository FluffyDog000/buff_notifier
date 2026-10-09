# Перенесено из csfloatpricesparcing/src/recency.py, коммит 4a7d61c, без изменений в логике.
# Править там и переносить заново, а не расходиться молча.
# Local extension: callers may bound trend sampling to reduce CPU on burstable
# servers. The default and pricing formulas remain the upstream values.
"""Older sales brought to today's prices, a window as long as the item needs,
and a resale price that owns up to how few sales it rests on.

Three things, all aimed at items that sell a hundred times a month rather than
a thousand, where a rung's top holds five sales in a fortnight:

Window by need. Sixteen days stays the window for any item that has a rung
with enough sales in it - liquid items are priced off fresh data exactly as
before. Only an item whose every rung falls short is read over 30 days, then
45.

Today's prices. A longer window on a falling market prices off last month:
$52.50 then, $51.50 now, and a median over both sits near $52 - paid for, and
never recovered. So the whole item's sales (far more of them than any rung
has) are made float-neutral the way `trend` does it - each divided by the
median of its own hundredth - and a line is drawn through them by age. Each
sale is then scaled by "level today / level on its day".

Asymmetric on purpose. Downward the line is followed in full: a fall taken
into account early costs at most a missed purchase. Upward it is capped by
what the last week actually traded at, and by five percent: a two-day spike
followed in full is a week-locked item bought at the top.

Then the median of a handful of sales is read with its error: the resale price
is the median less one standard error, which is a few cents at thirty sales
and half a dollar at five.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field, replace

# Windows tried, in days, after the configured one, for an item with no rung
# that has enough sales in it.
EXTEND_TO = (30.0, 45.0)
# Sales across the whole item before a line through them is believed.
MIN_TREND = 20
# How far one old sale may be moved: down to 85% (a fall bigger than that is
# for `max_drop` to refuse), up to 105% and never above the last week.
FALL_FLOOR = 0.85
RISE_CAP = 1.05
RECENT_DAYS = 7.0
MIN_RECENT = 5
BUCKET = 0.01
MIN_BUCKET = 3
# Pairs used for the slope. Theil-Sen is quadratic; past this many sales a
# regular subsample gives the same slope.
MAX_POINTS = 300
# Standard errors taken off the median, and the consistency constants that
# turn a median absolute deviation into a standard deviation and that into
# the standard error of a median.
SE_K = 1.0
MAD_TO_SIGMA = 1.4826
MEDIAN_SE = 1.2533


@dataclass
class Prepared:
    window: float                      # days actually used
    extended: bool = False             # longer than configured
    applied: bool = False              # prices were brought to today
    shift: float | None = None         # level today / level at window start - 1
    points: int = 0                    # sales the line rests on
    note: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _age(row) -> float | None:
    try:
        return float(row.get("age_days"))
    except (TypeError, ValueError):
        return None


def _price(row) -> float | None:
    try:
        v = float(row.get("price"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _float(row) -> float | None:
    for key in ("float_value", "float", "wear"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def within(rows, days: float) -> list[dict]:
    out = []
    for r in rows:
        a = _age(r)
        if a is not None and 0 <= a <= days:
            out.append(r)
    return out


def best_sample(rows, span, step: float) -> int:
    """Most sales any hundredth of float inside `span` holds - what the best
    rung's top would have to price from."""
    lo, hi = span if span else (0.0, 1.0)
    counts: dict[int, int] = {}
    for r in rows:
        f = _float(r)
        if f is None or _price(r) is None or not (lo <= f <= hi):
            continue
        k = int(f / step + 1e-9)
        counts[k] = counts.get(k, 0) + 1
    return max(counts.values(), default=0)


def choose_window(sales, base: float, min_sample: int, span,
                  step: float = BUCKET) -> tuple[float, bool]:
    """The configured window if it is enough, else the first longer one that
    is (or the longest, when none is)."""
    tried = [base] + [w for w in EXTEND_TO if w > base]
    for w in tried:
        if best_sample(within(sales, w), span, step) >= min_sample:
            return w, w > base
    return tried[-1], tried[-1] > base


def _theil_sen(points: list[tuple[float, float]], max_points: int = MAX_POINTS) -> tuple[float, float]:
    """(intercept at age 0, slope per day) of a line robust to outliers."""
    if len(points) > max_points:
        stride = len(points) / max_points
        points = [points[int(i * stride)] for i in range(max_points)]
    slopes = []
    for i in range(len(points)):
        xi, yi = points[i]
        for j in range(i + 1, len(points)):
            xj, yj = points[j]
            if abs(xj - xi) > 1e-9:
                slopes.append((yj - yi) / (xj - xi))
    b = st.median(slopes) if slopes else 0.0
    a = st.median([y - b * x for x, y in points])
    return a, b


def level_line(rows, max_points: int = MAX_POINTS):
    """The item's float-neutral price level against age, and the last week's.

    Returns (intercept, slope, points, recent_median, recent_count) or None
    when too few sales carry a level."""
    by_bucket: dict[int, list[float]] = {}
    for r in rows:
        f, p = _float(r), _price(r)
        if f is None or p is None or _age(r) is None:
            continue
        by_bucket.setdefault(int(f / BUCKET + 1e-9), []).append(p)
    level = {k: st.median(v) for k, v in by_bucket.items() if len(v) >= MIN_BUCKET}
    pts = []
    for r in rows:
        f, p, a = _float(r), _price(r), _age(r)
        if f is None or p is None or a is None:
            continue
        ref = level.get(int(f / BUCKET + 1e-9))
        if ref:
            pts.append((a, p / ref))
    if len(pts) < MIN_TREND:
        return None
    pts.sort()
    a, b = _theil_sen(pts, max_points)
    recent = [y for x, y in pts if x <= RECENT_DAYS]
    rec = st.median(recent) if len(recent) >= MIN_RECENT else None
    return a, b, len(pts), rec, len(recent)


def to_today(rows, window: float, max_points: int = MAX_POINTS) -> tuple[list[dict], Prepared]:
    """Copies of `rows` with each price brought to today's level; the original
    price kept as `raw_price`. Unchanged when no line can be drawn."""
    info = Prepared(window=window)
    line = level_line(rows, max_points)
    if line is None:
        info.note = "мало продаж у предмета для линии цены — без пересчёта"
        return [dict(r, raw_price=r.get("price")) for r in rows], info
    a, b, n, recent, _ = line
    info.points = n
    if a <= 0:
        return [dict(r, raw_price=r.get("price")) for r in rows], info
    out = []
    for r in rows:
        age, p = _age(r), _price(r)
        row = dict(r, raw_price=r.get("price"))
        if age is None or p is None:
            out.append(row)
            continue
        then = a + b * age
        if then <= 0:
            out.append(row)
            continue
        factor = a / then
        if factor < 1.0:
            factor = max(factor, FALL_FLOOR)
        elif factor > 1.0:
            # Up only as far as the last week actually traded, and 5% at most.
            cap = 1.0 if recent is None else min(RISE_CAP, max(recent / then, 1.0))
            factor = min(factor, cap)
        row["price"] = p * factor
        out.append(row)
    start = a + b * window
    info.applied = True
    info.shift = (a / start - 1.0) if start > 0 else None
    return out, info


def prepare(sales, params, span):
    """The sales a ladder should read, and the params to read them with.

    Returns (rows, params, Prepared): windowed to the chosen length, prices
    brought to today, and `window_days` set to the window actually used, since
    every rate divides by it."""
    window, extended = choose_window(sales, params.window_days,
                                     params.min_sample, span, params.top_step)
    rows = within(sales, window)
    rows, info = to_today(rows, window)
    info.extended = extended
    return rows, replace(params, window_days=window), info


def careful_median(prices, k: float = SE_K) -> tuple[float | None, float | None]:
    """(median less `k` standard errors, plain median)."""
    values = [float(p) for p in prices if p is not None]
    if not values:
        return None, None
    med = st.median(values)
    if len(values) < 2:
        return med, med
    mad = st.median([abs(v - med) for v in values])
    se = MEDIAN_SE * MAD_TO_SIGMA * mad / math.sqrt(len(values))
    return med - k * se, med
