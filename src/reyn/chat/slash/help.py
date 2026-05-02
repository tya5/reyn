"""/help — list all available slash commands with one-line summaries."""
from __future__ import annotations

from reyn.chat.slash import REGISTRY, reply, slash


# Built-ins handled outside the registry (intercepted by the TUI / REPL
# before reaching session._maybe_handle_slash). Listed here for discoverability.
_BUILTIN_HINTS: list[tuple[str, str]] = [
    ("quit", "Exit the chat (alias: /exit, Ctrl+D)"),
    ("exit", "Exit the chat (alias: /quit, Ctrl+D)"),
]


@slash("help", summary="Show this list of slash commands")
async def help_cmd(session: "object", args: str) -> None:
    rows: list[tuple[str, str]] = [
        (cmd.name, cmd.summary)
        for cmd in REGISTRY.all_commands()
        if not cmd.hidden
    ]
    rows.extend(_BUILTIN_HINTS)
    rows.sort(key=lambda r: r[0])

    width = max((len(name) for name, _ in rows), default=8)
    lines = ["Slash commands:"]
    for name, summary in rows:
        lines.append(f"  /{name:<{width}}  {summary}")
    lines.append("")
    lines.append("Tab opens the command palette with the same list.")

    await reply(session, "\n".join(lines))
