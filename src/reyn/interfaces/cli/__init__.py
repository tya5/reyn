"""Reyn CLI package — main() and parser construction.

Each subcommand lives in `cli.commands.<name>` and exposes:
  register(sub) — adds its argparse subparser (and sets `func` default)
  run(args)     — implementation invoked via args.func(args)
"""
from __future__ import annotations

import argparse

from reyn.llm.credentials import MissingCredentialsError

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
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except MissingCredentialsError as exc:
        # #2708 P3.2b: the CLI error boundary for the typed missing-cred error
        # raised at the LLM funnel (``recorded_acompletion``). Renders the same
        # actionable "no API key" message the removed per-surface startup gates
        # printed, then exits 1 — friendly stderr + exit, no raw litellm stack.
        import sys

        sys.stderr.write(f"Error: {exc.user_message()}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
