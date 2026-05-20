"""/help — list all available slash commands with one-line summaries."""
from __future__ import annotations

import textwrap

from reyn.chat.slash import REGISTRY, reply, slash

# Built-ins handled outside the registry. ``/quit`` + ``/exit`` were
# previously here but moved into ``reyn.chat.slash.quit`` (= wave-2 P3)
# so the slash palette can match them on ``/q`` / ``/ex`` prefixes.
# Keep this list as the future home for any TUI-intercepted commands
# that genuinely cannot land in the registry.
_BUILTIN_HINTS: list[tuple[str, str]] = []

# Target line width for pre-wrapping. The TUI body adds a 7-cell left
# Padding (``_BODY_INDENT_COLS`` in conversation.py) AND the conv pane
# / RichLog have their own ~3-cell horizontal padding+scrollbar overhead;
# 65 = 80-col terminal minus the combined ~15-cell budget. Wider terminals
# leave whitespace on the right (harmless); narrower terminals will still
# RichLog-wrap on top of our pre-wrap.
_TARGET_WIDTH = 65


@slash("help", summary="Show this list of slash commands")
async def help_cmd(session: "object", args: str) -> None:
    rows: list[tuple[str, str]] = [
        (cmd.name, cmd.summary)
        for cmd in REGISTRY.all_commands()
        if not cmd.hidden
    ]
    rows.extend(_BUILTIN_HINTS)
    rows.sort(key=lambda r: r[0])

    name_w = max((len(name) for name, _ in rows), default=8)
    # Data column starts after "  /" + name + "  ". Wrap continuations of
    # the summary align to this column so they read as continuations
    # rather than orphan commands.
    data_col = 2 + 1 + name_w + 2
    indent = " " * data_col

    lines = ["Slash commands:"]
    for name, summary in rows:
        prefix = f"  /{name:<{name_w}}  "
        wrapped = textwrap.fill(
            summary,
            width=_TARGET_WIDTH,
            initial_indent=prefix,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        ) if len(prefix) + len(summary) > _TARGET_WIDTH else prefix + summary
        lines.append(wrapped)
    lines.append("")
    footer = (
        "Type / to open the command palette. Tab inserts the highlighted "
        "command (and keeps the cursor); Enter inserts + submits."
    )
    lines.append(textwrap.fill(footer, width=_TARGET_WIDTH))

    await reply(session, "\n".join(lines))
