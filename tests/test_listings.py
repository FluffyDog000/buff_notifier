"""Listings read off a real answer (two listings of goods 24322, 07.10)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from notifier.listings import item_url, parse_listing, parse_page

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sell_order_24322.json")
                    .read_text(encoding="utf-8"))


def test_a_page_of_listings_is_read():
    page = parse_page(SAMPLE)
    assert page.total == 91
    first, second = page.listings
    assert first.id == "1094337592-18C8-137407483" and first.goods_id == 24322
    assert first.price == 29.56 and first.bargain_floor == 23.65
    assert abs(first.float_value - 0.2616334557533264) < 1e-12
    assert (first.paint_seed, first.paint_index) == (217, 1228)
    assert first.created_at == datetime(2026, 10, 7, 12, 45, 50, tzinfo=timezone.utc)
    assert first.listed and first.asset_id == "53981114096"
    assert second.paint_seed == 95 and second.price == 29.6


def test_stickers_and_their_premium_are_kept():
    raw = dict(SAMPLE["data"]["items"][0], sticker_premium=0.664794)
    raw["asset_info"] = dict(raw["asset_info"], info=dict(
        raw["asset_info"]["info"], stickers=[{"name": "Swallow", "wear": 0}]))
    x = parse_listing(raw)
    assert x.stickers == ["Swallow"] and x.sticker_premium == 0.664794


def test_a_record_without_a_price_or_a_float_does_not_break_the_page():
    assert parse_listing({"id": "x", "price": ""}) is None
    x = parse_listing({"id": "y", "price": "1.5", "state": 2})
    assert x.float_value is None and not x.listed and x.bargain_floor is None
    assert parse_page({"code": "OK", "data": {}}).listings == []


def test_the_item_page_link_matches_the_browsers():
    assert item_url("Desert Eagle | Mecha Industries (Minimal Wear)") == (
        "https://buff.market/market/goods/cs2/"
        "Desert%20Eagle%20%7C%20Mecha%20Industries%20%28Minimal%20Wear%29")
