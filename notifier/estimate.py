# Перенесено из csfloatpricesparcing/src/profit.py (estimate и его константы),
# коммит 4a7d61c, без изменений в логике. Править там и переносить заново.
"""What a skin is worth on CSFloat: the median of recent sales in its own
hundredth of float, or of the whole item when that hundredth has too few."""
from __future__ import annotations

import statistics
from typing import Sequence

BUCKET_MIN_SALES = 5
ITEM_MIN_SALES = 3


def estimate(sales: Sequence[dict], float_value: float | None) -> tuple[float | None, str]:
    """Median of recent sales in the skin's own hundredth of float, or of the
    whole item when that hundredth has too few. Returns (price, basis)."""
    prices = [float(s["price"]) for s in sales if s.get("price")]
    if float_value is not None:
        lo = int(float_value * 100) / 100.0
        near = [float(s["price"]) for s in sales
                if s.get("price") and s.get("float_value") is not None
                and lo <= float(s["float_value"]) < lo + 0.01]
        if len(near) >= BUCKET_MIN_SALES:
            return statistics.median(near), f"медиана {len(near)} продаж в {lo:.2f}–{lo + 0.01:.2f}"
    if len(prices) >= ITEM_MIN_SALES:
        return statistics.median(prices), f"медиана {len(prices)} продаж предмета"
    return None, "мало продаж для оценки"
