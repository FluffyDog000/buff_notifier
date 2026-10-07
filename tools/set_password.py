"""The web page's password: asked twice, stored in `.env` only as a hash.

    .venv/bin/python -m tools.set_password
"""
from __future__ import annotations

import getpass
import sys

from werkzeug.security import generate_password_hash

from notifier.config import ENV_PATH
from notifier.envfile import write_env


def main() -> int:
    pw = getpass.getpass("Новый пароль веб-панели: ")
    if len(pw) < 10:
        print("Слишком короткий: панель открыта в интернет, нужно 10 символов и больше.")
        return 1
    if pw != getpass.getpass("Ещё раз: "):
        print("Не совпали.")
        return 1
    write_env(ENV_PATH, {"WEB_PASSWORD_HASH": generate_password_hash(pw)})
    print(f"Сохранено в {ENV_PATH} (только хэш). Перезапуск не нужен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
