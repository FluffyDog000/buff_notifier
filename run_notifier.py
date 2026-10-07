#!/usr/bin/env python3
"""The notifier service: polls BuffMarket and alerts in Telegram.

    .venv/bin/python run_notifier.py
"""
from __future__ import annotations

import logging
import signal
import threading

from notifier import config
from notifier.pool import Pool

log = logging.getLogger("buff")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    pool = Pool()
    log.info("Запущен. Настройки: %s", config.SETTINGS_PATH)
    while not stop.is_set():
        try:
            wait = pool.tick()
        except Exception:  # noqa: BLE001 - one bad cycle must not end the service
            log.exception("Сбой цикла")
            wait = 60.0
        stop.wait(wait)
    log.info("Остановлен.")
    pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
