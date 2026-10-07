"""`.env`: secrets only, mode 600, read fresh every time.

The notifier re-reads it each cycle, so a session or a token changed from the
web page takes effect without a restart. Values are single-quoted on write,
which python-dotenv reads back literally (`;`, `#` and spaces included).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def write_env(path: Path, values: dict[str, str | None]) -> None:
    """Set (or, with None, remove) keys, keep every other line, mode 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    for k, v in values.items():
        if v is None:
            continue
        if "'" in v or "\n" in v:
            raise ValueError(f"{k}: кавычка или перевод строки в значении")
        lines.append(f"{k}='{v}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
