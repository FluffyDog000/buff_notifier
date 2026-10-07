"""Take the account's session from a browser's "Copy as cURL" into `.env`.

    python -m tools.set_session          # paste the curl, then Ctrl+D
    python -m tools.set_session req.txt

Reads the Cookie (`-b` or `-H 'Cookie: ...'`), X-CSRFToken and User-Agent,
writes them as BUFF_COOKIE, BUFF_CSRF and BUFF_USER_AGENT and leaves the
file at mode 600. Values are never printed - only cookie names and lengths.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from notifier.config import ENV_PATH
from notifier.envfile import write_env

KEYS = ("BUFF_COOKIE", "BUFF_CSRF", "BUFF_USER_AGENT")


def parse_curl(text: str) -> dict[str, str]:
    """BUFF_* values found in a curl command, bash or Windows cmd style."""
    if '^"' in text:
        # cmd: ^ escapes every special character and ends every line.
        text = re.sub(r"\^\r?\n", " ", text)
        text = re.sub(r"\^(.)", r"\1", text)
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    words = shlex.split(text)
    headers: dict[str, str] = {}
    cookie = ""
    for i, w in enumerate(words[:-1]):
        nxt = words[i + 1]
        if w in ("-H", "--header") and ":" in nxt:
            name, value = nxt.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        elif w in ("-b", "--cookie"):
            cookie = nxt.strip()
    out = {}
    cookie = cookie or headers.get("cookie", "")
    if cookie:
        out["BUFF_COOKIE"] = cookie
    if headers.get("x-csrftoken"):
        out["BUFF_CSRF"] = headers["x-csrftoken"]
    if headers.get("user-agent"):
        out["BUFF_USER_AGENT"] = headers["user-agent"]
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        text = Path(argv[0]).read_text(encoding="utf-8")
    else:
        print("Вставьте curl целиком, затем Enter и Ctrl+D:", file=sys.stderr)
        text = sys.stdin.read()
    try:
        values = parse_curl(text)
    except ValueError as e:
        print(f"Не разобрал curl: {e}. Нужен вариант «Copy as cURL (bash)».")
        return 1
    if "BUFF_COOKIE" not in values:
        print("В curl нет кук. Либо в этом браузере вы не вошли на buff.market, "
              "либо скопирован не тот запрос (нужен sell_order).")
        return 1
    names = [c.split("=", 1)[0].strip() for c in values["BUFF_COOKIE"].split(";") if c.strip()]
    print(f"Куки: {', '.join(names)}")
    for k in ("BUFF_CSRF", "BUFF_USER_AGENT"):
        print(f"{k}: {'есть, ' + str(len(values[k])) + ' символов' if k in values else 'нет'}")
    write_env(ENV_PATH, values)
    print(f"Записано в {ENV_PATH} (права 600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
