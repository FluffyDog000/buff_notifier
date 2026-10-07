"""Check the market search: is there a goods_id for this name?

    .venv/bin/python -m tools.find_goods "AK-47 | Redline (Field-Tested)"

Prints the goods_id when the search finds the exact name, otherwise what the
search answered - enough to fix the request if the site asks differently.
"""
from __future__ import annotations

import json
import sys

import requests

from notifier import buff, config, phases


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('Укажите название: python -m tools.find_goods "AK-47 | Redline (Field-Tested)"')
        return 1
    name = " ".join(argv)
    base = phases.split(name)[0]
    client = buff.from_config(config.load_settings(), config.load_secrets())
    try:
        body = client.search_goods(base)
    except (buff.BuffError, requests.RequestException) as e:
        print(f"Ошибка: {e}")
        r = client.last
        if r is not None:
            print(f"HTTP {r.status_code}: {r.text[:800]}")
        return 1
    gid = buff.match_goods(body, base)
    if gid is not None:
        print(f"goods_id {gid}: {base}")
        return 0
    items = (body.get("data") or {}).get("items") or []
    print(f"Точного совпадения нет. Результатов: {len(items)}")
    for it in items[:10]:
        print(f"  {it.get('id')}  {it.get('market_hash_name') or it.get('name')}")
    print("Начало ответа:\n" + json.dumps(body, ensure_ascii=False, indent=1)[:1500])
    return 1


if __name__ == "__main__":
    sys.exit(main())
