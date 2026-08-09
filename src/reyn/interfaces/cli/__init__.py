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
    # #3905: the #2708 P3.2b typed missing-cred error boundary was removed
    # along with the credential pre-check it caught (MissingCredentialsError,
    # reyn.llm.credentials) — an unnecessary hardcode (owner ruling). A
    # missing-credentials run now surfaces litellm's own typed exception
    # unmodified, uncaught here (a raw traceback), same as any other late
    # LLM-call failure this boundary never specially handled.
    args.func(args)


if __name__ == "__main__":
    main()
