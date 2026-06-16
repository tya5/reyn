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
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
