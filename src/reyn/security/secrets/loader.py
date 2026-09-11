"""Startup loader for ``~/.reyn/secrets.env`` (ADR-0030).

Called once at Reyn process startup (from ``config.load_config()``) so
that all components can read secrets via ``os.environ.get()`` without
any knowledge of the dotenv file.

Policy
------
* File absent  → OK, silently skip.
* Parse error  → ``logger.warning`` per bad line (#6145 — promoted from a
  silent-by-default ``warnings.warn``); skip that line.
* chmod 600 enforce → if the file is world-readable (mode & 0o004 != 0),
  emit a ``logger.warning`` and ``chmod 600`` automatically.
* Existing env NOT overridden → ``os.environ.setdefault()`` semantics:
  values already in ``os.environ`` (from the shell or earlier loaders)
  take priority over ``secrets.env``.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

_log = logging.getLogger(__name__)

_SECRETS_FILE = Path.home() / ".reyn" / "secrets.env"


def _default_secrets_path() -> Path:
    """Return the effective secrets.env path.

    The path is determined at *call time* (not module import time) so that
    ``REYN_SECRETS_PATH`` set by a pytest fixture is honoured without
    requiring module reload.  Production code never sets this variable.
    """
    override = os.environ.get("REYN_SECRETS_PATH")
    if override:
        return Path(override)
    return _SECRETS_FILE


def _enforce_permissions(path: Path) -> None:
    """Warn and auto-fix if the file is world-readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & stat.S_IROTH or mode & stat.S_IRGRP:
        # #6145 A (both branches below): was `warnings.warn(..., UserWarning)`
        # — silent outside `__main__` under Python's own default filter,
        # and even the visible category never reached the operator's
        # screen (`stderr: False / reyn.log: True`, architect's
        # measurement). A group/world-readable secrets file is a real
        # exposure on this machine; the operator needs this in the log,
        # not swallowed by the default warnings filter.
        _log.warning(
            "%s is readable by group/others (mode %s); auto-fixing to "
            "600. Review access controls on this machine.",
            path, oct(mode & 0o777),
        )
        try:
            path.chmod(0o600)
        except OSError as exc:
            _log.warning("Could not chmod %s to 600: %s", path, exc)


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
            # #6145 A (both branches below): same promotion as
            # `_enforce_permissions` above — an operator debugging a
            # secret that "didn't load" needs the exact line and reason
            # in the log, which `warnings.warn` never reliably delivered
            # (silent outside `__main__`, and never reached the screen
            # even when the category was visible).
            _log.warning(
                "secrets.env line %d: no '=' found, skipping",
                lineno,
            )
            continue
        key, _, raw_val = line.partition("=")
        key = key.strip()
        if not key:
            _log.warning(
                "secrets.env line %d: empty key, skipping",
                lineno,
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
        # #6145 A (both branches below): same promotion as the parse
        # errors above — a startup secrets load that silently produces
        # zero secrets needs this reason in the log.
        _log.warning("Could not read %s: %s", secrets_path, exc)
        return

    try:
        pairs = _parse_dotenv(text)
    except Exception as exc:  # pragma: no cover — belt-and-suspenders
        _log.warning("Unexpected error parsing %s: %s", secrets_path, exc)
        return

    for key, value in pairs:
        # setdefault semantics: don't override existing env vars.
        if key not in os.environ:
            os.environ[key] = value
