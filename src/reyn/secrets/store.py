"""Programmatic read/write API for ``~/.reyn/secrets.env`` (ADR-0030).

These functions are called by:
  * ``reyn secret {set,clear,rotate}`` CLI subcommands
  * Any future internal code that needs to persist a secret at runtime

The file is always written with chmod 600 after modification.
"""
from __future__ import annotations

import stat
from pathlib import Path

from .loader import _default_secrets_path, _parse_dotenv


def _secrets_path() -> Path:
    return _default_secrets_path()


def _read_raw(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_pairs(path: Path, pairs: list[tuple[str, str]]) -> None:
    """Write (key, value) pairs to the dotenv file and set chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}\n" for k, v in pairs]
    path.write_text("".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Best-effort; caller will have seen the warning from load.


def _read_pairs(path: Path) -> list[tuple[str, str]]:
    """Return the current (key, value) pairs from the dotenv file."""
    raw = _read_raw(path)
    return _parse_dotenv(raw)


def save_secret(key: str, value: str, path: Path | None = None) -> None:
    """Write or update *key* in ``~/.reyn/secrets.env``.

    Existing keys are updated in-place (preserving order of other keys).
    New keys are appended.
    """
    if not key:
        raise ValueError("Secret key must not be empty")
    secrets_path = path if path is not None else _secrets_path()
    pairs = _read_pairs(secrets_path)
    found = False
    updated: list[tuple[str, str]] = []
    for k, v in pairs:
        if k == key:
            updated.append((k, value))
            found = True
        else:
            updated.append((k, v))
    if not found:
        updated.append((key, value))
    _write_pairs(secrets_path, updated)


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """Return all secrets as a ``{key: value}`` dict.

    Later lines override earlier ones for duplicate keys (last-wins,
    consistent with dotenv conventions).
    """
    secrets_path = path if path is not None else _secrets_path()
    pairs = _read_pairs(secrets_path)
    return dict(pairs)


def clear_secret(key: str, path: Path | None = None) -> bool:
    """Remove *key* from ``~/.reyn/secrets.env``.

    Returns ``True`` if the key was present and removed, ``False`` if
    it was not found (no error — idempotent).
    """
    secrets_path = path if path is not None else _secrets_path()
    pairs = _read_pairs(secrets_path)
    original_count = len(pairs)
    remaining = [(k, v) for k, v in pairs if k != key]
    if len(remaining) == original_count:
        return False
    _write_pairs(secrets_path, remaining)
    return True


def list_secret_keys(path: Path | None = None) -> list[str]:
    """Return the list of KEY names stored in ``~/.reyn/secrets.env``.

    Preserves declaration order; duplicates are preserved (each occurrence
    is listed once).  Callers that need unique keys can deduplicate.
    """
    secrets_path = path if path is not None else _secrets_path()
    pairs = _read_pairs(secrets_path)
    # Return unique keys preserving first-occurrence order
    seen: set[str] = set()
    result: list[str] = []
    for k, _ in pairs:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result
