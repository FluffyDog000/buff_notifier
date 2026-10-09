"""Which listings make an alert."""
from datetime import datetime, timezone

import pytest

from notifier import config
from notifier.judge import Market, PriceSample, is_pattern_skin, judge, message
from notifier.listings import Listing

S = config.defaults()
NAME = "AK-47 | Redline (Field-Tested)"


def sales(bucket_prices: dict[float, float], n: int = 8) -> list[dict]:
    """n flat-priced sales a day apart in each hundredth (no trend to correct)."""
    rows = []
    for lo, price in bucket_prices.items():
        for i in range(n):
            rows.append({"price": price, "float_value": lo + 0.004, "age_days": 1.0 + i,
                         "paint_seed": 1})
    return rows


def lot(price, f, seed=1, idx=None, **kw):
    return Listing(id=f"L{price}-{f}", goods_id=1, price=price, float_value=f, paint_seed=seed,
                   paint_index=idx, created_at=datetime(2026, 10, 7, tzinfo=timezone.utc), **kw)


MARKET = Market.build(sales({0.15: 120.0, 0.16: 100.0, 0.17: 98.0}), S["window_days"])


def test_a_listing_well_below_its_hundredth_is_cheap():
    v = judge(lot(80.0, 0.1612), NAME, MARKET, S)
    assert v.kind == "cheap"
    assert v.expected == 100.0 and v.net == 98.0 and round(v.profit, 2) == 18.0
    assert "0.16–0.17" in v.basis and "16 дн" in v.basis


def test_a_small_discount_or_a_small_profit_is_not():
    assert judge(lot(90.0, 0.1612), NAME, MARKET, S).kind is None, "8% off"
    tiny = Market.build(sales({0.16: 1.0}), 16)
    assert judge(lot(0.5, 0.1612), NAME, tiny, S).kind is None, "48% off, $0.48"


def test_fees_and_rate_count_against_the_listing():
    s = dict(S, buff_fee=0.10)
    v = judge(lot(80.0, 0.1612), NAME, MARKET, s)
    assert round(v.cost, 2) == 88.0 and v.kind is None


def test_a_premium_float_at_an_ordinary_price_is_flagged():
    v = judge(lot(101.0, 0.1534), NAME, MARKET, S)
    assert v.kind == "float"
    assert v.normal == 100.0 and v.normal_lo == 0.16 and round(v.premium, 2) == 0.20
    assert "💎 Выгодный float" in message(lot(101.0, 0.1534), NAME, v, "https://x")


def test_a_premium_float_at_a_premium_price_is_not():
    assert judge(lot(112.0, 0.1534), NAME, MARKET, S).kind is None


def test_without_five_sales_in_the_hundredth_there_is_no_estimate_by_default():
    v = judge(lot(10.0, 0.4512), NAME, MARKET, S)
    assert v.kind is None and "мало продаж" in v.reason
    assert judge(lot(10.0, 0.4512), NAME, MARKET, dict(S, allow_item_median=True)).kind is None


def test_pattern_skins_are_skipped_or_flagged():
    fade = "★ Karambit | Fade (Factory New)"
    assert is_pattern_skin(fade) and is_pattern_skin("★ Bayonet | Case Hardened (Field-Tested)")
    assert not is_pattern_skin("★ Karambit | Amber Fade (Factory New)")
    assert not is_pattern_skin(NAME)
    assert judge(lot(80.0, 0.1612), fade, MARKET, S).reason == "паттерновый скин"
    v = judge(lot(80.0, 0.1612), fade, MARKET, dict(S, pattern_skins="notify"))
    assert v.kind == "cheap" and "Паттерновый" in message(lot(80.0, 0.1612), fade, v, "x")


def test_a_doppler_phase_keeps_only_its_own_listings():
    name = "★ Bayonet | Doppler Phase 2 (Factory New)"
    assert judge(lot(80.0, 0.0112, idx=418), name, MARKET, S).reason == "другая фаза"


def test_the_message_is_compact_and_uses_moscow_time():
    x = lot(80.0, 0.1612, seed=217, bargain_floor=70.0, stickers=["Swallow"])
    text = message(x, NAME, judge(x, NAME, MARKET, S), "https://buff.market/x")
    for part in ("Дешевле рынка", NAME, "Float 0.16120", "$80.00", "$100.00", "$18.00",
                 "float 0.16–0.17", "16 дн.", "https://buff.market/x", "07.10 03:00 МСК"):
        assert part in text, part
    assert len(text.splitlines()) == 8 and len(text) < 400
    assert "UTC" not in text and "Лот " not in text and "Swallow" not in text
    x.created_at = x.created_at.replace(hour=23, minute=30)
    assert "08.10 02:30 МСК" in message(x, NAME, judge(x, NAME, MARKET, S), 'https://x')


def test_legacy_either_mode_requires_similar_sales_while_min_also_caps_price():
    market = Market.build(sales({.15: 120., .16: 100., .17: 98.}), 16)
    s = dict(S, price_basis="either", discount_basis="median", min_discount=.2, float_signal=False)
    v = judge(lot(95., .155), NAME, market, s)
    assert v.kind == "cheap" and v.expected == 120. and "float" in v.reference
    assert judge(lot(95., .155), NAME, market, dict(s, price_basis="min")).kind is None
    # The legacy mode can no longer bypass the comparable sample requirement.
    v = judge(lot(80., .455), NAME, market, s)
    assert v.kind is None and "недостаточно данных" in v.reason


def test_discount_and_return_are_distinct_and_boundary_is_inclusive():
    s = dict(S, price_basis="bucket", discount_basis="median", min_discount=.2, float_signal=False)
    assert judge(lot(80., .165), NAME, MARKET, s).kind == "cheap"
    assert judge(lot(80.01, .165), NAME, MARKET, s).kind is None
    assert judge(lot(80., .165), NAME, MARKET, dict(s, discount_basis="net")).kind is None
    assert judge(lot(80., .165), NAME, MARKET, dict(s, min_profit_pct=.23)).kind is None
    assert judge(lot(80., .165), NAME, MARKET, dict(s, min_profit_pct=.2)).kind == "cheap"


def test_vanilla_knife_is_valued_without_float_and_never_has_float_signal():
    market = Market.build([dict(price=100., float_value=None, age_days=i) for i in range(5)], 16)
    x = lot(75., None)
    v = judge(x, "★ Survival Knife", market, S)
    assert v.kind == "cheap" and v.expected == 100. and market.window == 16
    assert "ванильный нож" in message(x, "★ Survival Knife", v, "https://x")
    assert "🔬" not in message(x, "★ Survival Knife", v, "https://x")
    assert judge(x, NAME, market, S).reason == "нет float"


def test_a_thin_hundredth_inflated_by_rare_sales_is_held_to_the_item_median():
    """Five-SeveN | Heat Treated FT, 07.10: seven sales in 0.33-0.34 with a few
    dear patterns among them put that hundredth at $5.30, while the item sells
    at $2.59. A listing at $2.44 is no bargain."""
    rows = [{"price": 2.59, "float_value": 0.205 + (i % 9) * 0.01, "age_days": 1.0 + i % 14}
            for i in range(90)]
    rows += [{"price": p, "float_value": 0.335, "age_days": 2.0 + i}
             for i, p in enumerate((2.6, 3.0, 5.3, 5.3, 6.0, 9.0, 26.0))]
    m = Market.build(rows, 14)
    name = "AK-47 | Slate (Field-Tested)"
    x = lot(2.44, 0.33442)
    v = judge(x, name, m, dict(S, min_profit_usd=0.5))
    assert v.kind is None and v.expected == 2.59
    assert judge(x, name, m, dict(S, min_profit_usd=0.5, price_basis="item")).kind is None
    conservative = judge(x, name, m, dict(S, min_profit_usd=0.5, price_basis="bucket"))
    assert conservative.kind is None and conservative.expected < 5.3, \
        "uncertainty in a small noisy sample must reduce its apparent profit"


def test_a_worse_float_is_not_cheap_against_the_item_median():
    v = judge(lot(85.0, 0.1712), NAME, MARKET, S)
    assert v.expected == 98.0 and v.kind is None, "its own hundredth sells at 98, not 100"


def test_heat_treated_is_a_pattern_skin():
    assert is_pattern_skin("Five-SeveN | Heat Treated (Field-Tested)")


@pytest.mark.parametrize("mode", ["min", "bucket", "either", "item"])
def test_awp_sparse_float_never_borrows_expensive_better_float_sales(mode):
    rows = sales({.08: 45.5}, n=20)
    rows += [dict(price=p, float_value=f, age_days=2.) for p, f in
             zip([31.83, 31.89, 33.15, 34.02], [.1343, .1361, .1388, .1365])]
    v = judge(lot(35.07, .13569), "AWP | Chromatic Aberration (Minimal Wear)",
              Market.build(rows, 14), dict(S, price_basis=mode, allow_item_median=True))
    assert v.kind is None and v.expected is None and v.sample_count == 4
    assert "недостаточно данных" in v.reason


def test_float_matches_are_close_even_within_one_hundredth():
    m = Market.build(sales({.16: 100.}, n=10), 14)
    # .164 is in the same hundredth as .1699, but too far away to compare.
    assert judge(lot(50., .1699), NAME, m, S).expected is None
    assert judge(lot(50., .165), NAME, m, S).expected == 100.
    perfect = Market.build([dict(r, float_value=.009) for r in sales({.0: 100.}, n=10)], 14)
    assert judge(lot(50., .0002), NAME, perfect, S).expected is None


@pytest.mark.parametrize("name", ["★ Bayonet | Doppler (Factory New)",
                                 "★ Bayonet | Doppler Phase 2 (Factory New)"])
def test_phase_sales_are_never_mixed(name):
    own = [dict(r, paint_index=419) for r in sales({.0: 100.}, n=5)]
    other = [dict(r, paint_index=415) for r in sales({.0: 500.}, n=20)]
    v = judge(lot(75., .004, idx=419), name, Market.build(own + other, 14), S)
    assert v.kind == "cheap" and v.expected == 100. and v.sample_count == 5
    m = Market.build(own[:4] + other, 14)
    assert judge(lot(75., .004, idx=419), name, m, S).expected is None
    assert "неизвестна фаза" in judge(lot(75., .004), "★ Bayonet | Doppler (Factory New)", m, S).reason


def test_pattern_requires_five_close_float_sales_of_exact_seed():
    name = "★ Bayonet | Case Hardened (Field-Tested)"
    s = dict(S, pattern_skins="notify")
    own = sales({.16: 100.}, n=5)
    other = [dict(r, paint_seed=2) for r in sales({.16: 1000.}, n=20)]
    v = judge(lot(75., .165), name, Market.build(own + other, 14), s)
    assert v.kind == "cheap" and v.expected == 100. and v.sample_count == 5
    assert "seed 1" in v.basis
    assert judge(lot(75., .165), name, Market.build(own[:4] + other, 14), s).expected is None
    assert "неизвестен seed" in judge(lot(75., .165, seed=None), name, MARKET, s).reason


def test_window_extends_for_this_float_instead_of_a_more_liquid_hundredth():
    rows = sales({.08: 300.}, n=20)
    own = [dict(price=100., float_value=.165, age_days=age) for age in [1., 3., 18., 20., 25.]]
    market = Market.build(rows + own, 14)
    model, matched = market.comparable(lot(75., .165), None, False, False)
    assert model.window == 30 and len(matched) == 5
    v = judge(lot(75., .165), NAME, market, S)
    assert v.kind is None and "свежих похожих продаж 2/5" in v.reason
    old = [dict(r, age_days=31. + i) for i, r in enumerate(own)]
    market = Market.build(rows + old, 14)
    model, matched = market.comparable(lot(75., .165), None, False, False)
    assert model.window == 45 and len(matched) == 5
    assert judge(lot(75., .165), NAME, market, S).kind is None
    old[-1]["age_days"] = 46.
    assert judge(lot(75., .165), NAME, Market.build(rows + old, 14), S).expected is None


def test_estimate_reserve_grows_with_spread_and_shrinking_sample():
    def sample(prices):
        return PriceSample.build([dict(price=p, age_days=1.) for p in prices], percentile=.5)
    small = sample([80., 90., 100., 110., 120.])
    large = sample([80., 90., 100., 110., 120.] * 8)
    wide = sample([60., 80., 100., 120., 140.])
    assert small.median == large.median == wide.median == 100.
    assert 0 < wide.expected < small.expected < large.expected < 100.
    assert small.confidence == "низкая" and large.confidence == "высокая"
    assert sample([100.] * 8).expected == 100.
    assert sample([100.] * 8 + [10000.]).expected == 100., "one expensive sale must not inflate the estimate"


def test_noisy_mp9_sample_cannot_use_unadjusted_median_for_profit():
    prices = [8.87, 9.68, 10.35, 10.71, 11.85, 12.82]
    rows = [dict(price=p, float_value=.264, age_days=1.) for p in prices]
    v = judge(lot(8.39, .26365), "MP9 | Latte Rush (Field-Tested)", Market.build(rows, 14),
              dict(S, buff_fee=.01, min_profit_pct=.15, price_basis="bucket"))
    assert v.kind is None and v.raw_median == pytest.approx(10.53)
    assert v.expected < 9.5 and v.profit_pct < .15


def test_neighbour_premium_uses_conservative_price_and_same_phase():
    name = "★ Bayonet | Doppler (Factory New)"
    rows = [dict(r, paint_index=419) for r in sales({.15: 120.}, n=8)]
    rows += [dict(r, paint_index=415) for r in sales({.16: 80.}, n=8)]
    assert judge(lot(80., .154, idx=419), name, Market.build(rows, 16),
                 dict(S, min_discount=.5)).kind is None


def test_stickers_do_not_add_a_premium_or_change_the_comparable_selection():
    plain = judge(lot(80., .165), NAME, MARKET, S)
    stickered = judge(lot(80., .165, stickers=["Expensive"], sticker_premium=1000.), NAME, MARKET, S)
    assert plain.expected == stickered.expected and plain.sample_count == stickered.sample_count


def test_float_only_mode_suppresses_cheap_alerts_but_keeps_float_opportunities():
    s = dict(S, cheap_signal=False)
    cheap = judge(lot(80., .165), NAME, MARKET, s)
    assert cheap.kind is None and "выключен" in cheap.reason
    assert judge(lot(101., .1534), NAME, MARKET, s).kind == "float"
    # Even a deep discount must be labelled as a float opportunity in this mode.
    assert judge(lot(50., .1534), NAME, MARKET, s).kind == "float"
    assert judge(lot(75., None), "★ Survival Knife",
                 Market.build([dict(price=100., float_value=None, age_days=1.)] * 5, 14), s).kind is None


def test_legacy_price_settings_are_migrated_without_loosening_selection(tmp_path):
    path = tmp_path / "settings.json"
    assert config.save_settings({"price_basis": "item"}, path)["price_basis"] == "item"
    assert config.load_settings(path)["price_basis"] == "min"
    config.save_settings({"price_basis": "either"}, path)
    assert config.load_settings(path)["price_basis"] == "bucket"


def test_old_expensive_sales_cannot_lift_price_above_recent_lower_quartile():
    # P250: most of the fortnight's observations are older and dearer.
    recent = [14.115,14.212,15.731,15.898,16.543]
    rows = [dict(price=p,raw_price=p,age_days=1.,float_value=.068) for p in recent]
    rows += [dict(price=21.,raw_price=21.,age_days=12.,float_value=.068)] * 18
    sample = PriceSample.build(rows)
    assert sample.median == 21. and sample.expected == pytest.approx(14.212)
    assert sample.recent_count == 5
    assert PriceSample.build(rows, percentile=.5).expected == pytest.approx(15.731)
    rows = [dict(r,age_days=10.) for r in rows]
    assert PriceSample.build(rows) is None, "plenty of old sales cannot substitute for fresh evidence"


def test_recent_sale_minimum_applies_to_both_signals_and_the_neighbour():
    own = sales({.15:120.}, n=8)
    neighbours = sales({.16:100.}, n=8)
    neighbours = [dict(r,age_days=10. if i >= 4 else r['age_days']) for i,r in enumerate(neighbours)]
    market = Market.build(own + neighbours, 16)
    assert judge(lot(101.,.1534),NAME,market,dict(S,cheap_signal=False)).kind is None
    assert judge(lot(101.,.1534),NAME,market,dict(S,cheap_signal=False,min_recent_sales=4)).kind == 'float'
    own = [dict(r,age_days=20.) for r in own]
    assert judge(lot(10.,.1534),NAME,Market.build(own,16),S).kind is None


def test_float_lookup_reuses_index_without_changing_boundaries_or_phase_matching():
    rows = sales({.16:100.,.17:200.}, n=8)
    market = Market.build(rows,16)
    for f in (.16,.161,.164,.169,.17,.174):
        expected = [r for r in rows if int(r['float_value']*100)==int(f*100)
                    and abs(r['float_value']-f)<=.005+1e-12]
        assert market.close_rows(f) == sorted(expected,key=lambda r:r['float_value'])


def test_float_purchase_price_is_compared_to_recent_ordinary_prices():
    own = [dict(price=140.,age_days=1.,float_value=.154)] * 5
    neighbours = [dict(price=80.,age_days=1.,float_value=.164)] * 5
    neighbours += [dict(price=100.,age_days=12.,float_value=.164)] * 10
    v = judge(lot(90.,.154),NAME,Market.build(own+neighbours,16),dict(S,cheap_signal=False))
    assert v.kind is None, "ordinary prices have fallen to 80; paying 90 is no longer ordinary"
