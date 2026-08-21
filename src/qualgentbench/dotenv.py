"""Load ``./.env`` into the process environment. Package-level because every entry
point needs it, not just the click CLI. Never overrides an existing variable —
explicit env and CI secrets win."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = ".env"


def load_dotenv(path: str | os.PathLike[str] = DEFAULT_ENV_FILE) -> None:
    """Load KEY=VALUE lines into os.environ (no override). Missing file is a
    no-op; the default path is relative to the cwd."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted value: keep the content between the quotes verbatim,
            # including any literal '#'.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted: an inline comment starts at the first '#' preceded by
            # whitespace (dotenv convention).
            for i, ch in enumerate(value):
                if ch == "#" and (i == 0 or value[i - 1] in " \t"):
                    value = value[:i]
                    break
            value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
