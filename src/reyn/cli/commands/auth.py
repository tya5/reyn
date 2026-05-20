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
import os
import sys
import webbrowser
from datetime import datetime

# Spinner frame strings — fixed width (3 chars) so successive ``\r``-anchored
# redraws fully overwrite the previous frame without leaving residue. The
# empty-trailer frame ("   ") creates a brief visual pause that confirms
# the animation is still running rather than freezing on three dots.
_SPINNER_FRAMES = (".  ", ".. ", "...", "   ")
_SPINNER_INTERVAL_SECONDS = 0.5


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


def _box_user_code(code: str, *, indent: str = "     ") -> str:
    """Render *code* inside a unicode box for visual emphasis.

    Empty / missing codes degrade gracefully — the user still sees the
    URL and a `(no user code)` placeholder so the auth flow surface
    explains itself instead of dropping the line.
    """
    if not code:
        return f"{indent}(no user code)"
    inner = f"  {code}  "
    border = "─" * len(inner)
    return (
        f"{indent}┌{border}┐\n"
        f"{indent}│{inner}│\n"
        f"{indent}└{border}┘"
    )


def _open_browser_or_skip(url: str) -> None:
    """Offer to auto-open *url* in the user's browser.

    Skips silently when:
      - stdin is not a TTY (= pipe, script, CI run) so `input()` would
        hang or raise EOFError.
      - The ``REYN_AUTH_NO_BROWSER`` env var is set (= explicit opt-out
        for users on remote shells / SSH without DISPLAY).

    Otherwise prompts the user to press Enter, then calls
    ``webbrowser.open()``. Browser-launch failures are swallowed —
    the URL is already on screen so the manual fallback works.
    KeyboardInterrupt during the Enter prompt propagates as cancel.
    """
    if not sys.stdin.isatty():
        return
    if os.environ.get("REYN_AUTH_NO_BROWSER"):
        return
    try:
        print(
            "Press Enter to open the URL in your browser (Ctrl+C to cancel)...",
            end="",
            file=sys.stderr,
            flush=True,
        )
        input()
    except EOFError:
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — manual fallback URL is already printed
        pass


def _print_scope_advertisement(provider_cfg) -> None:
    """Print the OAuth scopes this device-grant flow is about to request.

    Issue #291 Priority 3 #7: previously reyn silently sent ``scope=...``
    to the provider's device_authorization endpoint with no client-side
    visibility. The provider's consent screen still lists scopes, but
    surfacing them in the CLI first gives the user a chance to abort
    before any network round-trip — and creates a paper trail in shell
    history / CI logs of exactly what was requested.

    Skips silently when ``provider.scopes`` is empty (= provider does
    not request any scopes, or the user is using a public-API token).
    """
    scopes = getattr(provider_cfg, "scopes", None) or []
    if not scopes:
        return
    print(
        f"Requesting scopes for {provider_cfg.name!r}: {', '.join(scopes)}",
        file=sys.stderr,
    )


def _print_user_action(info: dict) -> None:
    """Display the device code + verification URI for the user to act on.

    Renders a 3-section layout:
      1. The verification URL (preferring ``verification_uri_complete``
         when the provider supplied one — that variant embeds the
         user_code so a single click handles both steps).
      2. The user_code in a unicode box (= phishing protection per RFC
         8628 §3.3.1 still asks the user to verify the code matches
         even when ``verification_uri_complete`` is used).
      3. The deadline (= ``expires_in`` in minutes, when present) so
         the user knows how long they have before the device code
         expires server-side.

    Falls through to ``_open_browser_or_skip`` which handles the
    Enter-to-open prompt + TTY / env-var skips.
    """
    user_code = info.get("user_code", "")
    verification_uri = info.get("verification_uri", "")
    verification_uri_complete = info.get("verification_uri_complete")
    expires_in = info.get("expires_in")
    url_to_show = verification_uri_complete or verification_uri

    print(file=sys.stderr)
    print("To authenticate:", file=sys.stderr)
    print(file=sys.stderr)
    print("  1. Open this URL in your browser:", file=sys.stderr)
    print(f"     {url_to_show}", file=sys.stderr)
    print(file=sys.stderr)
    print("  2. Verify the code matches:", file=sys.stderr)
    print(file=sys.stderr)
    print(_box_user_code(user_code), file=sys.stderr)
    print(file=sys.stderr)

    if isinstance(expires_in, int) and expires_in > 0:
        minutes = max(1, expires_in // 60)
        unit = "minute" if minutes == 1 else "minutes"
        print(f"  Code expires in {minutes} {unit}.", file=sys.stderr)
        print(file=sys.stderr)

    _open_browser_or_skip(url_to_show)

    # The Ctrl+C hint is part of the static line so non-TTY contexts
    # (= pipe, CI capture) still see it — ``_animated_wait`` on TTY
    # adds the spinner on the next line.
    print("Waiting for approval (Ctrl+C to cancel)...", file=sys.stderr)


async def _animated_wait(seconds: float) -> None:
    """Show an animated dot spinner on stderr for *seconds*.

    The spinner cycles through ``_SPINNER_FRAMES`` every
    ``_SPINNER_INTERVAL_SECONDS``, using ``\\r`` to overwrite the
    previous frame on the same line. Two-space indent matches the
    other CLI bullets (= the box drawing + expires_in lines) so the
    spinner reads as "this is the same flow, still waiting".

    Falls back to a plain ``asyncio.sleep`` when stderr is not a TTY
    (= CI, pipes, log capture) — animation is noise in those contexts.
    """
    if not sys.stderr.isatty():
        await asyncio.sleep(seconds)
        return

    idx = 0
    elapsed = 0.0
    while elapsed < seconds:
        frame = _SPINNER_FRAMES[idx % len(_SPINNER_FRAMES)]
        print(f"\r  {frame}", end="", file=sys.stderr, flush=True)
        await asyncio.sleep(_SPINNER_INTERVAL_SECONDS)
        elapsed += _SPINNER_INTERVAL_SECONDS
        idx += 1


def _print_slow_down_notice(new_interval: float) -> None:
    """Surface the OAuth server's slow_down hint to the user.

    Clears the spinner line first (TTY only) so the notice prints
    cleanly, then logs the new interval as a permanent line. The
    spinner resumes on the next ``_animated_wait`` cycle below.
    """
    if sys.stderr.isatty():
        # 60 chars is enough to clear the spinner + any slow_down residue
        # without making the line jitter on narrow terminals.
        print("\r" + " " * 60 + "\r", end="", file=sys.stderr, flush=True)
    print(
        f"  Server requested slower polling — interval now {new_interval:.0f}s.",
        file=sys.stderr,
    )


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

    _print_scope_advertisement(provider_cfg)

    async def _run() -> None:
        try:
            token = await device_grant_flow(
                provider_cfg,
                events=events,
                on_user_action=_print_user_action,
                wait_fn=_animated_wait,
                on_slow_down=_print_slow_down_notice,
            )
        except DeviceGrantError as exc:
            # Terminate the spinner line cleanly before printing the error
            # (otherwise the error overlays the last spinner frame on TTY).
            if sys.stderr.isatty():
                print("", file=sys.stderr)
            print(f"Authentication failed: {exc}", file=sys.stderr)
            sys.exit(2)
        if sys.stderr.isatty():
            print("", file=sys.stderr)
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


def _classify_token_status(delta_seconds: float) -> str:
    """Map an ``expires_at - now`` delta (seconds) to a 3-state label.

    Issue #291 Priority 3 #9: previously the 2-state label ``valid`` /
    ``near-expiry`` collapsed ``expired`` (= already past) into the same
    bucket as ``near-expiry`` (= about to expire), making it impossible
    to distinguish "needs refresh now" from "needs re-auth now" from
    the list output.

    The thresholds match the OAuth refresh buffer (``oauth.py:_REFRESH_BUFFER_SECONDS``):
      - ``expired`` :  delta <  0    (= past the expires_at timestamp)
      - ``near-expiry`` : 0 ≤ delta < 60   (= within refresh-on-next-use window)
      - ``valid`` : delta ≥ 60   (= no refresh needed yet)
    """
    if delta_seconds < 0:
        return "expired"
    if delta_seconds < 60:
        return "near-expiry"
    return "valid"


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
        status = _classify_token_status(delta.total_seconds())
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
