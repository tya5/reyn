"""File-based TTL cache for registry responses.

Cache location: ``~/.reyn/registry-cache/<encoded_key>.json``
TTL: 24 hours (mtime check — no background eviction).

Public API:
  ``get(key) -> dict | None``   — None for miss or expired entry.
  ``set(key, data)``            — Write (create parent dirs automatically).

Design notes:
- Keys are URL-safe encoded (``urllib.parse.quote(key, safe="")``).
- Corrupt / unreadable cache files are silently treated as misses.
- ``set`` uses write-then-rename (atomic swap) to avoid partial writes.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

_TTL_SECONDS = 24 * 3600  # 24 h


def _cache_dir() -> Path:
    return Path.home() / ".reyn" / "registry-cache"


def _key_to_path(key: str) -> Path:
    safe = urllib.parse.quote(key, safe="")
    return _cache_dir() / f"{safe}.json"


def get(key: str) -> dict | None:
    """Return cached data for *key*, or ``None`` on miss / expiry / corrupt."""
    path = _key_to_path(key)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    age = time.time() - stat.st_mtime
    if age > _TTL_SECONDS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt file → treat as miss; will be overwritten on next set().
        return None


def set(key: str, data: dict) -> None:
    """Write *data* to the cache under *key*.

    Uses atomic write-then-rename to avoid partial reads on concurrent
    access (rare in practice, but cheap to guard against).
    """
    path = _key_to_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file, then rename atomically.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        # Best-effort: remove the temp file on failure and propagate.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
