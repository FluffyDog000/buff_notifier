# Перенесено из csfloatpricesparcing/tests/test_recency.py, коммит 4a7d61c, без изменений в логике.
# Править там и переносить заново, а не расходиться молча.
"""Pricing for thin items: window by need, prices brought to today,
a median read with its error."""
import logging

import pytest

from notifier import recency

logging.disable(logging.WARNING)


def _item(level_now, level_month_ago, n=60, days=30.0, floats=(0.155, 0.205, 0.305)):
    """An item whose float-neutral level moves in a straight line from a month
    ago to today, sales spread evenly, three floats with fixed premiums."""
    premium = {0.155: 1.05, 0.205: 1.00, 0.305: 0.95}
    rows = []
    for i in range(n):
        age = days * i / (n - 1)
        level = level_now + (level_month_ago - level_now) * age / days
        f = floats[i % len(floats)]
        rows.append({"age_days": age, "float_value": f,
                     "price": round(level * premium.get(f, 1.0), 2)})
    return rows


def test_on_a_falling_market_old_sales_come_down_to_today():
    rows = _item(51.50, 52.50)
    out, info = recency.to_today(rows, 30.0)
    assert info.applied and info.shift == pytest.approx(51.50 / 52.50 - 1, abs=0.003)
    # A month-old 0.205 sale at $52.50 reads as today's $51.50 - and so does
    # every other one of its float, whatever its age.
    for r in out:
        if r["float_value"] == 0.205:
            assert r["price"] == pytest.approx(51.50, abs=0.06), r


def test_on_a_rising_market_old_sales_rise_no_further_than_last_week():
    rows = _item(52.00, 50.00)
    out, info = recency.to_today(rows, 30.0)
    for r in out:
        assert r["price"] <= r["raw_price"] * recency.RISE_CAP + 1e-9
    # The last week traded near $52, so a month-old $50 sale may rise toward
    # it - but not past what the week shows.
    old = [r for r in out if r["float_value"] == 0.205 and r["age_days"] > 25]
    assert old and all(50.0 < r["price"] <= 52.0 + 0.06 for r in old)


def test_a_spike_without_a_week_behind_it_is_not_followed_up():
    rows = _item(50.00, 50.00)
    # Two days of sales far above the line, nothing else recent.
    rows = [r for r in rows if r["age_days"] > 7]
    rows += [{"age_days": 0.5, "float_value": 0.205, "price": 60.0},
             {"age_days": 1.0, "float_value": 0.205, "price": 60.0}]
    out, _ = recency.to_today(rows, 30.0)
    for r in out:
        if r["age_days"] > 7:
            assert r["price"] == pytest.approx(r["raw_price"], abs=1e-9), \
                "fewer than five sales in the last week: no lift at all"


def test_too_few_sales_for_a_line_leave_prices_alone():
    rows = _item(51.5, 52.5, n=12)
    out, info = recency.to_today(rows, 30.0)
    assert not info.applied
    assert all(r["price"] == r["raw_price"] for r in out)


def test_a_fall_is_followed_no_further_than_fifteen_percent():
    rows = _item(40.00, 60.00)
    out, _ = recency.to_today(rows, 30.0)
    assert min(r["price"] / r["raw_price"] for r in out) >= recency.FALL_FLOOR - 1e-9


def test_the_careful_median_owns_up_to_a_small_sample():
    five = [50.5, 51.0, 52.0, 52.5, 53.0]
    careful, plain = recency.careful_median(five)
    assert plain == 52.0 and careful < 52.0 - 0.4
    thirty = [50.5, 51.0, 52.0, 52.5, 53.0] * 6
    careful30, _ = recency.careful_median(thirty)
    assert 52.0 - careful30 < (52.0 - careful) / 2, "six times the sales, less than half the cut"


def test_a_liquid_item_keeps_its_window_and_a_thin_one_gets_longer():
    liquid = _item(50, 50, n=200, days=45.0, floats=(0.205,))
    assert recency.choose_window(liquid, 16.0, 8, (0.15, 0.38)) == (16.0, False)
    thin = _item(50, 50, n=30, days=45.0, floats=(0.155, 0.205, 0.255))
    w, extended = recency.choose_window(thin, 16.0, 8, (0.15, 0.38))
    assert extended and w in (30.0, 45.0)


