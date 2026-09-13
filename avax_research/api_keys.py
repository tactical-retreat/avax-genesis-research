"""Glacier (Avalanche Data API) keys, loaded from the environment or a local, uncommitted file.

Keys are never in the repository. Lookup order:

1. `GLACIER_API_KEYS` (comma-separated) or `GLACIER_API_KEY`
2. the file named by `GLACIER_API_KEYS_FILE`
3. `glacier_api_keys.txt` in the current directory (gitignored; copy `glacier_api_keys.example.txt`)

The file holds one key per line; `#` starts a comment. No keys means unauthenticated requests, which work
but get a much lower rate limit. Each key has its own quota and the client rotates through them, so
several keys trace proportionally faster. See the README ("API keys").
"""

import os
from pathlib import Path

DEFAULT_KEYS_FILE = Path("glacier_api_keys.txt")


def read_keys_file(path: Path) -> list[str]:
    keys = []
    for line in path.read_text().splitlines():
        key = line.split("#", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


def load_api_keys(path: str | Path | None = None) -> list[str]:
    """Return the configured Glacier API keys (possibly empty)."""
    env = os.environ.get("GLACIER_API_KEYS") or os.environ.get("GLACIER_API_KEY")
    if env:
        return [k.strip() for k in env.split(",") if k.strip()]
    file = Path(path or os.environ.get("GLACIER_API_KEYS_FILE") or DEFAULT_KEYS_FILE)
    return read_keys_file(file) if file.is_file() else []
