"""Parse bulk credentials without ever including submitted values in errors."""
import csv
from urllib.parse import quote

from .accounts import validate_proxy
from .browser_login import browser_proxy


def proxy_line(value: str) -> str:
    value = value.strip()
    if value in ("", "-", "direct"):
        return ""
    if "://" not in value:
        parts = value.split(":", 3)
        if len(parts) == 2:
            value = "http://" + value
        elif len(parts) == 4:
            host, port, user, password = parts
            value = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            raise ValueError("Прокси: URL, host:port или host:port:login:password.")
    value = validate_proxy(value)
    browser_proxy(value)
    return value


def parse_bulk(accounts_text: str, proxies_text: str = "", shared_proxy=False) -> list[dict]:
    rows, seen = [], set()
    for number, line in enumerate(accounts_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            if ";" in line:
                fields = next(csv.reader([line], delimiter=";", strict=True))
                if len(fields) not in (2, 3):
                    raise ValueError("Нужно login;password или login;password;proxy.")
                username, password = fields[:2]
                proxy = proxy_line(fields[2]) if len(fields) == 3 else None
            else:
                username, separator, password = line.partition(":")
                if not separator:
                    raise ValueError("Нужно login:password или login;password;proxy.")
                proxy = None
            username = username.strip()
            if not username or not password or len(username) > 255 or len(password) > 512:
                raise ValueError("Нужны логин до 255 и пароль до 512 символов.")
            if username.lower() in seen:
                raise ValueError("Логин повторяется в списке.")
            seen.add(username.lower())
            rows.append(dict(line=number, username=username, password=password, proxy=proxy))
        except csv.Error:
            raise ValueError(f"Строка {number}: проверьте кавычки и разделители.") from None
        except ValueError as e:
            raise ValueError(f"Строка {number}: {e}") from None
    if not rows or len(rows) > 199:
        raise ValueError("Вставьте от 1 до 199 аккаунтов; общий предел — 200 вместе с основным.")
    proxy_rows = [(n, line) for n, line in enumerate(proxies_text.splitlines(), 1) if line.strip()]
    if proxy_rows and any(row["proxy"] is not None for row in rows):
        raise ValueError("Задайте прокси в строках аккаунтов либо отдельным списком.")
    if proxy_rows:
        proxies = []
        for number, line in proxy_rows:
            try:
                proxies.append(proxy_line(line))
            except ValueError as e:
                raise ValueError(f"Прокси, строка {number}: {e}") from None
        if shared_proxy and len(proxies) == 1:
            proxies *= len(rows)
        if len(proxies) != len(rows):
            raise ValueError("Число прокси должно совпадать с числом аккаунтов. Для одного общего прокси отметьте соответствующий флажок.")
        for row, proxy in zip(rows, proxies):
            row["proxy"] = proxy
    for row in rows:
        row["proxy"] = row["proxy"] or ""
    return rows
