"""A page of BuffMarket listings, read.

The answer to `sell_order` (sample in tests/fixtures):

    {"code": "OK", "data": {"items": [...], "total_count": 91, ...}, "msg": null}

and each listing:

    id                    "1094337592-18C8-137407483"   the listing
    goods_id              24322                         the item
    price                 "29.56"                       asking price, a string
    lowest_bargain_price  "23.65"                       the floor of an offer
    created_at            1791377150                    unix seconds, UTC
    state / state_text    1 / "Listed"
    asset_info.paintwear  "0.2616334557533264"          float, a string
    asset_info.info.paintseed / paintindex / stickers[]
    sticker_premium       0.66 or null

Currency of `price` is assumed USD (what buff.market shows); not yet
confirmed from the answer itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

SITE = "https://buff.market"
LISTED = 1


def signal_key(listing_id: str, goods_id: int, float_value, paint_seed) -> str:
    """Remote order IDs append a changing request/account suffix to the lot ID.

    Keep the original ID for history. Float/seed and goods scope the stable
    portion so that other physical skins of the same item remain distinct.
    """
    remote = re.fullmatch(r"(\d+-[0-9A-Fa-f]+)-\d+", listing_id)
    if remote:
        return json.dumps(["remote", goods_id, remote[1], float_value, paint_seed], separators=(",", ":"))
    return "listing:" + listing_id


@dataclass
class Listing:
    id: str
    goods_id: int
    price: float
    float_value: float | None
    paint_seed: int | None
    paint_index: int | None
    created_at: datetime | None
    listed: bool = True
    bargain_floor: float | None = None
    stickers: list[str] = field(default_factory=list)
    sticker_premium: float | None = None
    asset_id: str | None = None


@dataclass
class Page:
    listings: list[Listing]
    total: int | None


def _num(v, cast=float):
    try:
        return cast(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _when(v) -> datetime | None:
    t = _num(v)
    return datetime.fromtimestamp(t, timezone.utc) if t else None


def parse_listing(raw: dict) -> Listing | None:
    """None for a record without an id or a price - nothing to act on."""
    price = _num(raw.get("price"))
    if not raw.get("id") or not price:
        return None
    asset = raw.get("asset_info") or {}
    info = asset.get("info") or {}
    return Listing(
        id=str(raw["id"]),
        goods_id=_num(raw.get("goods_id") or asset.get("goods_id"), int),
        price=price,
        float_value=_num(asset.get("paintwear")),
        paint_seed=_num(info.get("paintseed"), int),
        paint_index=_num(info.get("paintindex"), int),
        created_at=_when(raw.get("created_at")),
        listed=raw.get("state", LISTED) == LISTED,
        bargain_floor=_num(raw.get("lowest_bargain_price")) if raw.get("can_bargain") else None,
        stickers=[s.get("name", "") for s in info.get("stickers") or []],
        sticker_premium=_num(raw.get("sticker_premium")),
        asset_id=asset.get("assetid"),
    )


def parse_page(body: dict) -> Page:
    data = body.get("data") or {}
    rows = data.get("items") or []
    listings = [x for x in (parse_listing(r) for r in rows if isinstance(r, dict)) if x]
    return Page(listings=listings, total=_num(data.get("total_count"), int))


def item_url(market_hash_name: str) -> str:
    """The item page, as the browser's Referer named it."""
    return f"{SITE}/market/goods/cs2/{quote(market_hash_name, safe='')}"
