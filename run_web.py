#!/usr/bin/env python3
"""The web page, on 127.0.0.1 only: outside it is reached through Caddy (HTTPS).

    .venv/bin/python run_web.py            # port 5050, or WEB_PORT
"""
from __future__ import annotations

import logging
import os
import signal

from waitress import serve

from web.app import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    def stop(*_):
        app.extensions["browser_logins"].close()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        serve(app, host="127.0.0.1", port=int(os.environ.get("WEB_PORT", "5050")),
              threads=4, ident=None)
    finally:
        app.extensions["browser_logins"].close()
