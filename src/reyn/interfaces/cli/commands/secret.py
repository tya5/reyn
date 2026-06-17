"""`reyn secret` — universal secret management CLI (ADR-0030).

Subcommands:
  set <KEY>[=<VALUE>]   Write or update a secret in ~/.reyn/secrets.env
  list                  Show KEY names + status; values are never displayed
  clear <KEY>           Remove a single secret
  rotate <KEY>          Alias for `set` with explicit audit intent

Each mutating command emits a P6 audit event (value is fully masked).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from reyn.core.events.events import EventLog

# Module-level event log for secret audit events (P6).
# CLI commands emit into this log; callers can subscribe for persistence.
_audit_log = EventLog()


def _get_audit_log() -> EventLog:
    """Return the module-level audit EventLog.  Tests may subscribe to it."""
    return _audit_log


def register(sub) -> None:
    p = sub.add_parser("secret", help="Manage secrets stored in ~/.reyn/secrets.env")
    ssub = p.add_subparsers(dest="secret_cmd", metavar="<subcommand>")
    ssub.required = True
    p.set_defaults(func=_no_subcommand)

    # --- set ---
    s = ssub.add_parser(
        "set",
        help="Set a secret (prompts for value if omitted from KEY=VALUE)",
    )
    s.add_argument(
        "key_value",
        metavar="KEY[=VALUE]",
        help="Key name or KEY=VALUE pair. If only KEY is given, value is prompted interactively.",
    )
    s.set_defaults(func=run_set)

    # --- list ---
    ssub.add_parser(
        "list",
        help="List secret KEY names and status (values are never shown)",
    ).set_defaults(func=run_list)

    # --- clear ---
    c = ssub.add_parser("clear", help="Remove a secret from ~/.reyn/secrets.env")
    c.add_argument("key", metavar="KEY", help="Key to remove")
    c.set_defaults(func=run_clear)

    # --- rotate ---
    r = ssub.add_parser(
        "rotate",
        help="Rotate a secret (alias for set with rotation audit intent)",
    )
    r.add_argument(
        "key_value",
        metavar="KEY[=VALUE]",
        help="Key name or KEY=VALUE pair. If only KEY is given, value is prompted interactively.",
    )
    r.set_defaults(func=run_rotate)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print("Usage: reyn secret <subcommand>  (set | list | clear | rotate)", file=sys.stderr)
    sys.exit(1)


def _parse_key_value(raw: str) -> tuple[str, str | None]:
    """Split ``KEY`` or ``KEY=VALUE`` into (key, value_or_None)."""
    if "=" in raw:
        key, _, value = raw.partition("=")
        return key.strip(), value
    return raw.strip(), None


def _prompt_value(key: str) -> str:
    """Interactively prompt for a secret value (hidden input)."""
    try:
        return getpass.getpass(f"Value for {key}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)


def run_set(args: argparse.Namespace) -> None:
    """Set a secret. Emits ``secret_set`` audit event (value masked)."""
    from reyn.security.secrets.store import save_secret

    key, value = _parse_key_value(args.key_value)
    if not key:
        print("Error: KEY must not be empty.", file=sys.stderr)
        sys.exit(1)
    if value is None:
        value = _prompt_value(key)

    save_secret(key, value)
    _audit_log.emit("secret_set", key=key, value_masked="***")
    print(f"Secret '{key}' saved to ~/.reyn/secrets.env")


def run_list(args: argparse.Namespace) -> None:
    """List secret KEY names and their status (set / unset in os.environ)."""
    from reyn.security.secrets.store import list_secret_keys

    keys = list_secret_keys()
    if not keys:
        print("No secrets stored in ~/.reyn/secrets.env")
        return

    # Header
    col_key = max(len(k) for k in keys)
    col_key = max(col_key, 4)  # at least "KEY "
    header = f"{'KEY':<{col_key}}  STATUS"
    print(header)
    print("─" * len(header))
    for key in keys:
        in_env = key in os.environ
        status = "set" if in_env else "stored (not yet in env)"
        print(f"{key:<{col_key}}  {status}")


def run_clear(args: argparse.Namespace) -> None:
    """Remove a secret. Emits ``secret_cleared`` audit event."""
    from reyn.security.secrets.store import clear_secret

    key = args.key.strip()
    if not key:
        print("Error: KEY must not be empty.", file=sys.stderr)
        sys.exit(1)

    removed = clear_secret(key)
    if removed:
        _audit_log.emit("secret_cleared", key=key)
        print(f"Secret '{key}' removed from ~/.reyn/secrets.env")
    else:
        print(f"Secret '{key}' not found in ~/.reyn/secrets.env (nothing changed)")


def run_rotate(args: argparse.Namespace) -> None:
    """Rotate a secret. Emits ``secret_rotated`` audit event (value masked)."""
    from reyn.security.secrets.store import save_secret

    key, value = _parse_key_value(args.key_value)
    if not key:
        print("Error: KEY must not be empty.", file=sys.stderr)
        sys.exit(1)
    if value is None:
        value = _prompt_value(key)

    save_secret(key, value)
    _audit_log.emit("secret_rotated", key=key, value_masked="***")
    print(f"Secret '{key}' rotated in ~/.reyn/secrets.env")
