"""`reyn auth` — OAuth credential management (FP-0016 Components B+C).

Subcommands:
  login <provider>  — run RFC 8628 device authorization grant flow
                       and persist the resulting token.
  list              — list known OAuth token keys.
  revoke <key>      — remove an OAuth token from the store.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime


def register(sub) -> None:
    p = sub.add_parser("auth", help="Manage OAuth credentials (login, list, revoke)")
    asub = p.add_subparsers(dest="auth_cmd", metavar="<subcommand>")
    asub.required = True
    p.set_defaults(func=_no_subcommand)

    # login
    login = asub.add_parser(
        "login",
        help="Authenticate with an OAuth provider via RFC 8628 device grant",
    )
    login.add_argument(
        "provider",
        metavar="PROVIDER",
        help="Provider name from reyn.yaml auth.providers (e.g. github, google)",
    )
    login.add_argument(
        "--save-as",
        metavar="KEY",
        default=None,
        help="Store the resulting token under this key (default: PROVIDER name)",
    )
    login.set_defaults(func=run_login)

    # list
    asub.add_parser(
        "list",
        help="List OAuth token keys present in the store",
    ).set_defaults(func=run_list)

    # revoke
    rev = asub.add_parser(
        "revoke",
        help="Remove an OAuth token from the store (does not call provider)",
    )
    rev.add_argument("key", metavar="KEY", help="Token key to remove")
    rev.set_defaults(func=run_revoke)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print(
        "Usage: reyn auth <subcommand>  (login | list | revoke)",
        file=sys.stderr,
    )
    sys.exit(1)


def _print_user_action(info: dict) -> None:
    """Display the device code + verification URI for the user to act on."""
    print(file=sys.stderr)
    print("To authenticate, open this URL in your browser:", file=sys.stderr)
    if info.get("verification_uri_complete"):
        print(f"  {info['verification_uri_complete']}", file=sys.stderr)
    else:
        print(f"  {info['verification_uri']}", file=sys.stderr)
        print(f"  and enter code: {info['user_code']}", file=sys.stderr)
    print(file=sys.stderr)
    print("Waiting for approval...", file=sys.stderr)


def run_login(args: argparse.Namespace) -> None:
    """Run device grant flow + persist the token."""
    from reyn.config import load_config
    from reyn.events.events import EventLog
    from reyn.secrets import (
        DeviceGrantError,
        OAuthProviderConfig,
        device_grant_flow,
        save_oauth_token,
    )

    config = load_config()
    providers = config.auth.providers if hasattr(config, "auth") else {}
    provider_cfg: OAuthProviderConfig | None = providers.get(args.provider)
    if provider_cfg is None:
        print(
            f"Error: provider {args.provider!r} not configured in "
            f"reyn.yaml auth.providers. Known providers: "
            f"{sorted(providers.keys()) or '(none)'}",
            file=sys.stderr,
        )
        sys.exit(1)

    key = args.save_as or args.provider
    events = EventLog()

    async def _run() -> None:
        try:
            token = await device_grant_flow(
                provider_cfg,
                events=events,
                on_user_action=_print_user_action,
            )
        except DeviceGrantError as exc:
            print(f"Authentication failed: {exc}", file=sys.stderr)
            sys.exit(2)
        save_oauth_token(key, token)
        print(
            f"Saved OAuth token under key {key!r}. "
            f"Expires at {token.expires_at.isoformat()}.",
            file=sys.stderr,
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)


def run_list(args: argparse.Namespace) -> None:
    """List token keys in the OAuth store. Values are never printed."""
    from reyn.secrets import list_oauth_token_keys, load_oauth_token

    keys = list_oauth_token_keys()
    if not keys:
        print("(no OAuth tokens stored)", file=sys.stderr)
        return
    now = datetime.now().astimezone()
    for key in keys:
        token = load_oauth_token(key)
        if token is None:
            print(f"  {key}: <malformed>")
            continue
        delta = token.expires_at - now
        status = "valid" if delta.total_seconds() > 60 else "near-expiry"
        print(f"  {key}: {status}, expires {token.expires_at.isoformat()}")


def run_revoke(args: argparse.Namespace) -> None:
    """Remove a key from the local OAuth store."""
    from reyn.secrets import clear_oauth_token

    removed = clear_oauth_token(args.key)
    if removed:
        print(f"Revoked {args.key!r} from local OAuth store.", file=sys.stderr)
    else:
        print(f"No token under key {args.key!r}.", file=sys.stderr)
        sys.exit(1)
