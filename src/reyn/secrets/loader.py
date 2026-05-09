"""Startup loader for ``~/.reyn/secrets.env`` (ADR-0030).

Called once at Reyn process startup (from ``config.load_config()``) so
that all components can read secrets via ``os.environ.get()`` without
any knowledge of the dotenv file.

Policy
------
* File absent  → OK, silently skip.
* Parse error  → :class:`UserWarning` emitted per bad line; skip that line.
* chmod 600 enforce → if the file is world-readable (mode & 0o004 != 0),
  emit a warning and ``chmod 600`` automatically.
* Existing env NOT overridden → ``os.environ.setdefault()`` semantics:
  values already in ``os.environ`` (from the shell or earlier loaders)
  take priority over ``secrets.env``.
"""
from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path

_SECRETS_FILE = Path.home() / ".reyn" / "secrets.env"


def _default_secrets_path() -> Path:
    return _SECRETS_FILE


def _enforce_permissions(path: Path) -> None:
    """Warn and auto-fix if the file is world-readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & stat.S_IROTH or mode & stat.S_IRGRP:
        warnings.warn(
            f"{path} is readable by group/others (mode {oct(mode & 0o777)}); "
            "auto-fixing to 600. Review access controls on this machine.",
            UserWarning,
            stacklevel=3,
        )
        try:
            path.chmod(0o600)
        except OSError as exc:
            warnings.warn(
                f"Could not chmod {path} to 600: {exc}",
                UserWarning,
                stacklevel=3,
            )


def _parse_dotenv(text: str) -> list[tuple[str, str]]:
    """Parse a dotenv-format string into (key, value) pairs.

    Handles:
      KEY=value
      KEY="quoted value"
      KEY='single quoted'
      # comment lines
      blank lines
    """
    pairs: list[tuple[str, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warnings.warn(
                f"secrets.env line {lineno}: no '=' found, skipping: {raw_line!r}",
                UserWarning,
                stacklevel=4,
            )
            continue
        key, _, raw_val = line.partition("=")
        key = key.strip()
        if not key:
            warnings.warn(
                f"secrets.env line {lineno}: empty key, skipping: {raw_line!r}",
                UserWarning,
                stacklevel=4,
            )
            continue
        # Strip inline comments on unquoted values
        val = raw_val.strip()
        # Handle quoted values
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        else:
            # Strip trailing inline comment (# after whitespace)
            comment_pos = val.find(" #")
            if comment_pos != -1:
                val = val[:comment_pos].strip()
        pairs.append((key, val))
    return pairs


def load_secrets_to_environ(path: Path | None = None) -> None:
    """Load ``~/.reyn/secrets.env`` into ``os.environ`` (no override).

    Safe to call multiple times — already-set env vars are not changed.
    Missing file is silently ignored. Parse errors emit warnings but do
    not abort startup.

    Parameters
    ----------
    path:
        Override the default ``~/.reyn/secrets.env`` path.  Used by tests
        to point at a temp file.
    """
    secrets_path = path if path is not None else _default_secrets_path()

    if not secrets_path.exists():
        return

    _enforce_permissions(secrets_path)

    try:
        text = secrets_path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.warn(
            f"Could not read {secrets_path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
        return

    try:
        pairs = _parse_dotenv(text)
    except Exception as exc:  # pragma: no cover — belt-and-suspenders
        warnings.warn(
            f"Unexpected error parsing {secrets_path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
        return

    for key, value in pairs:
        # setdefault semantics: don't override existing env vars.
        if key not in os.environ:
            os.environ[key] = value
