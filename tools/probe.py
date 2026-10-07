"""One request to BuffMarket's item page endpoint, and what came back.

    python -m tools.probe 5777            # Desert Eagle | Mecha Industries (MW)
    python -m tools.probe 5777 --size 3 --save probe.json

Prints the status, the headers that matter for pacing, the shape of the
answer and its first listings in full. The session's cookie and token are
never printed; cookies the site sets back are shown by name only.
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from notifier import config
from notifier.buff import BuffError, LoginRequired, from_config
from notifier.listings import parse_page

PACING = ("retry-after", "ratelimit", "x-ratelimit", "cf-", "server", "content-encoding",
          "content-length", "content-type")


def key_paths(obj, prefix: str = "", depth: int = 4) -> list[str]:
    """Every key path down to `depth`, lists read through their first element."""
    out = []
    if depth < 0:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(f"{p}: {type(v).__name__}")
            out += key_paths(v, p, depth - 1)
    elif isinstance(obj, list) and obj:
        out += key_paths(obj[0], prefix + "[0]", depth - 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("goods_id", type=int)
    ap.add_argument("--size", type=int, default=3, help="лотов на странице")
    ap.add_argument("--save", help="сохранить ответ целиком в файл")
    a = ap.parse_args(argv)

    sec = config.load_secrets()
    if not sec["BUFF_COOKIE"]:
        print("BUFF_COOKIE в .env пуст - BuffMarket без входа ответит Login Required.")
    client = from_config(config.load_settings(), sec)
    # The raw answer is wanted here even when it is an error, so the request
    # goes through the client's session but is read by hand.
    try:
        client.sell_orders(a.goods_id, page_size=a.size)
        print("Клиент: ответ принят.")
    except LoginRequired as e:
        print(f"Клиент: {e}")
    except BuffError as e:
        print(f"Клиент: {e}; retry_after={e.retry_after}")
    except requests.RequestException as e:
        print(f"Сеть: {e}")
        return 1
    r = client.last

    print(f"HTTP {r.status_code}, {len(r.content)} байт после распаковки")
    for k, v in r.headers.items():
        if k.lower() == "set-cookie":
            print(f"  {k}: {v.split('=', 1)[0]}=***")
        elif any(k.lower().startswith(p) for p in PACING):
            print(f"  {k}: {v}")
    try:
        body = r.json()
    except ValueError:
        print("Не JSON. Начало ответа:\n" + r.text[:1500])
        return 1
    if a.save:
        with open(a.save, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=1)
        print(f"Сохранено в {a.save}")

    print("\nФорма ответа:")
    print("\n".join("  " + p for p in key_paths(body)))
    if body.get("code") == "OK":
        page = parse_page(body)
        print(f"\nЛотов всего: {page.total}, на странице разобрано: {len(page.listings)}")
        for x in page.listings:
            when = x.created_at.strftime("%d.%m %H:%M UTC") if x.created_at else "?"
            print(f"  {x.id}  ${x.price:.2f}  float {x.float_value}  seed {x.paint_seed}  "
                  f"выставлен {when}" + (f"  наклейки: {', '.join(x.stickers)}" if x.stickers else ""))
    print("\nНачало ответа:")
    print(json.dumps(body, ensure_ascii=False, indent=1)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
