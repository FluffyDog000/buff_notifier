"""Telegram: send a text, find the chat a bot has been written from.

The token lives in the URL of every call, so an error is logged with the
token cut out - a failed send must not put it into the journal.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("buff.telegram")
API = "https://api.telegram.org"


class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: float = 20.0):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, **data) -> dict:
        try:
            r = requests.post(f"{API}/bot{self.token}/{method}", data=data, timeout=self.timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise RuntimeError(str(e).replace(self.token, "***") if self.token else str(e)) from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram: {body.get('description', r.status_code)}")
        return body

    def send(self, text: str) -> bool:
        """No parse_mode: names like `★ M9 | Fade` must not be read as markup."""
        if not self.configured():
            return False
        try:
            self._call("sendMessage", chat_id=self.chat_id, text=text,
                       disable_web_page_preview="true")
            return True
        except RuntimeError as e:
            log.warning("Telegram: не отправлено: %s", e)
            return False

    def chats(self) -> list[dict]:
        """Chats that wrote to the bot lately: where its alerts could go."""
        out = {}
        for u in self._call("getUpdates").get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                out[chat["id"]] = {"id": chat["id"], "title": chat.get("title")
                                   or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
                                   or chat.get("username") or ""}
        return list(out.values())
