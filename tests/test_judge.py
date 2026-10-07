"""Which listings make an alert."""
from datetime import datetime, timezone

from notifier import config
from notifier.judge import Market, is_pattern_skin, judge, message
from notifier.listings import Listing

S = config.defaults()
NAME = "AK-47 | Redline (Field-Tested)"


def sales(bucket_prices: dict[float, float], n: int = 8) -> list[dict]:
    """n flat-priced sales a day apart in each hundredth (no trend to correct)."""
    rows = []
    for lo, price in bucket_prices.items():
        for i in range(n):
            rows.append({"price": price, "float_value": lo + 0.004, "age_days": 1.0 + i})
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
    assert "дороже соседней 0.16" in message(lot(101.0, 0.1534), NAME, v, "https://x")


def test_a_premium_float_at_a_premium_price_is_not():
    assert judge(lot(112.0, 0.1534), NAME, MARKET, S).kind is None


def test_without_five_sales_in_the_hundredth_there_is_no_estimate_by_default():
    v = judge(lot(10.0, 0.4512), NAME, MARKET, S)
    assert v.kind is None and "мало продаж" in v.reason
    assert judge(lot(10.0, 0.4512), NAME, MARKET, dict(S, allow_item_median=True)).kind == "cheap"


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


def test_the_message_says_what_the_brief_asks_for():
    x = lot(80.0, 0.1612, seed=217, bargain_floor=70.0, stickers=["Swallow"])
    text = message(x, NAME, judge(x, NAME, MARKET, S), "https://buff.market/x")
    for part in ("Дешевле рынка", NAME, "Float 0.16120", "паттерн 217", "Swallow",
                 "$80.00", "торг от $70.00", "$100.00", "$98.00", "$18.00",
                 "медиана 8 продаж", "https://buff.market/x", "Лот L80.0-0.1612"):
        assert part in text, part
