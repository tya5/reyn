"""Reyn CLI package — main() and parser construction.

Each subcommand lives in `cli.commands.<name>` and exposes:
  register(sub) — adds its argparse subparser (and sets `func` default)
  run(args)     — implementation invoked via args.func(args)
"""
from __future__ import annotations

import argparse

from .commands import ALL as _COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reyn",
        description="Agent OS MVP — LLM-driven phase execution",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    for module in _COMMANDS:
        module.register(sub)
    return parser


def main() -> None:
    # #3671: closes the ``import`` stage. Everything before this line is the
    # interpreter starting plus the import tree — measured at 1.75s for
    # ``litellm`` alone here, and the owner's machine spends ~3.4x longer in
    # the same region. Without this mark that time lands in ``unaccounted``,
    # where the largest phase of startup looks like a mystery.
    from reyn.runtime.startup_timing import mark_cli_reached  # noqa: PLC0415

    mark_cli_reached()
    parser = build_parser()
    args = parser.parse_args()
    # #3869: name the process before the subcommand runs, so anything the
    # operator inspects afterwards (ps, Activity Monitor, a crash post-mortem)
    # says "reyn:chat" instead of "python3.12". Set AFTER parsing because the
    # subcommand is the whole payload of the name, and before args.func because
    # that call is where a long-running surface (chat, serve) stops returning.
    from reyn.runtime.proctitle import set_process_title  # noqa: PLC0415

    set_process_title(getattr(args, "command", None))
    # #3905: the #2708 P3.2b typed missing-cred error boundary (and the
    # hardcoded _PROVIDER_ENV_VARS pre-check it caught) was removed — an
    # unnecessary hardcode (owner ruling). But owner's OTHER standing
    # directive (UX/predictability) means "no boundary at all" isn't the
    # right default either (lead-coder review correction): a missing
    # ANTHROPIC_API_KEY would surface as a raw Python traceback instead of
    # a one-line message, purely because reyn no longer knows the env var
    # NAME — a UX regression this narrower catch avoids without
    # reintroducing any hardcoded enumeration (isinstance, not a lookup
    # table; every provider litellm supports is covered by construction,
    # not a fixed list reyn must keep in sync).
    #
    # litellm only raises AuthenticationError for SOME providers on a
    # missing key (anthropic measured) — others (openai measured) raise
    # InternalServerError instead, with no litellm-specific common base
    # between the two (verified via the MRO: only openai.APIError is
    # shared). This narrows, not restores, the old boundary: anthropic-shaped
    # misses get the friendly one-liner, openai-shaped ones still traceback
    # — an HONEST partial improvement, not the old "all providers" claim.
    #
    # ``sys.modules`` gate BEFORE the litellm import (not just "import
    # litellm.exceptions and see"): importing ``litellm.exceptions`` pulls in
    # ALL of litellm (measured directly: 1.76s cold, matching #3671's own
    # cold-import figure) — paying that for an exception that was never
    # litellm's in the first place would tax every unrelated late failure
    # (a bug in file-handling code, say) with an irrelevant multi-second
    # startup-cost regression. If litellm was never imported, this exception
    # cannot possibly be a litellm one — skip the check entirely. If it WAS
    # imported (an LLM call was actually attempted), the re-import is a
    # sys.modules cache hit (measured: ~0.7 microseconds, not the 1.76s
    # cold cost) — free.
    import sys

    try:
        args.func(args)
    except Exception as exc:
        if "litellm" in sys.modules:
            from litellm.exceptions import AuthenticationError  # noqa: PLC0415

            if isinstance(exc, AuthenticationError):
                sys.stderr.write(f"Error: {exc}\n")
                sys.exit(1)
        raise


if __name__ == "__main__":
    main()
