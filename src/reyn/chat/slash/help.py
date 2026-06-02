"""/help — slash command listing with optional per-command focus mode."""
from __future__ import annotations

import textwrap

from reyn.chat.slash import REGISTRY, reply, reply_error, slash, suggest_for_unknown

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


def _render_command_focus(name: str) -> str:
    """Build a focused help panel for a single command.

    Surfaces name + aliases + summary + usage (if the command opted in
    via :class:`SlashCommand.usage`). When the typed name doesn't
    resolve, returns the "unknown" branch with ``suggest_for_unknown``
    suggestions so the user can recover without re-running ``/help``.
    """
    cmd = REGISTRY.get(name)
    if cmd is None:
        suggestions = suggest_for_unknown(name)
        sugg_list = " ".join(f"/{s}" for s in suggestions)
        return f"unknown command /{name} — did you mean: {sugg_list}?"

    lines: list[str] = [f"/{cmd.name}"]
    if cmd.aliases:
        alias_list = ", ".join(f"/{a}" for a in cmd.aliases)
        lines.append(f"  aliases: {alias_list}")
    # Summary, with continuation-indent for the wrap so multi-line
    # summaries read as continuations of the same field.
    summary_indent = "  summary: "
    summary_cont = " " * len(summary_indent)
    lines.append(textwrap.fill(
        cmd.summary,
        width=_TARGET_WIDTH,
        initial_indent=summary_indent,
        subsequent_indent=summary_cont,
        break_long_words=False,
        break_on_hyphens=False,
    ))
    if cmd.usage:
        lines.append(f"  usage:   {cmd.usage}")
    # Hidden-command hint: ``/help <hidden>`` is the way to discover
    # commands that intentionally don't appear in the bare ``/help``
    # list (matrix / donut / zen). Surface this once per focus view.
    if cmd.hidden:
        lines.append("  (hidden — not listed in /help)")
    if cmd.see_also:
        lines.append(f"  see also: {', '.join(cmd.see_also)}")
    return "\n".join(lines)


@slash(
    "help",
    summary="Slash command help — list all, or focus on one",
    usage="/help [<cmd>]",
)
async def help_cmd(session: "object", args: str) -> None:
    arg = (args or "").strip()
    if arg:
        # Per-command focus mode. Strip a leading "/" if the user
        # typed ``/help /find`` — the slash is harmless here, the
        # registry only stores names without one.
        target = arg.lstrip("/")
        if not target:
            await reply_error(session, "usage: /help [<cmd>]")
            return
        panel = _render_command_focus(target)
        # Unknown-command branch deserves the error styling — same
        # signalling as a dispatched-but-unknown command.
        if panel.startswith("unknown"):
            await reply_error(session, panel)
        else:
            await reply(session, panel)
        return

    rows: list[tuple[str, str, tuple[str, ...]]] = [
        (cmd.name, cmd.summary, cmd.aliases)
        for cmd in REGISTRY.all_commands()
        if not cmd.hidden
    ]
    rows.extend((name, summary, ()) for name, summary in _BUILTIN_HINTS)
    rows.sort(key=lambda r: r[0])

    name_w = max((len(name) for name, _, _ in rows), default=8)
    # Data column starts after "  /" + name + "  ". Wrap continuations of
    # the summary align to this column so they read as continuations
    # rather than orphan commands.
    data_col = 2 + 1 + name_w + 2
    indent = " " * data_col

    lines = ["Slash commands:"]
    for name, summary, aliases in rows:
        # Append alias hint inline so the user can discover shorthand names
        # without having to run ``/help <cmd>`` for each command individually.
        alias_hint = (
            "  (also: " + ", ".join(f"/{a}" for a in aliases) + ")"
            if aliases else ""
        )
        display_summary = summary + alias_hint
        prefix = f"  /{name:<{name_w}}  "
        wrapped = textwrap.fill(
            display_summary,
            width=_TARGET_WIDTH,
            initial_indent=prefix,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        ) if len(prefix) + len(display_summary) > _TARGET_WIDTH else prefix + display_summary
        lines.append(wrapped)
    lines.append("")
    footer = (
        "Type / to open the command palette. Tab inserts the highlighted "
        "command (and keeps the cursor); Enter inserts + submits. "
        "Type /help <cmd> for command-specific detail."
    )
    lines.append(textwrap.fill(footer, width=_TARGET_WIDTH))

    await reply(session, "\n".join(lines))
