"""/memory slash command — inspect the project memory store from chat.

Read-only subcommands:

  /memory list                 — list every memory entry (name + type + summary)
  /memory view <name>          — print the full body of a single entry

Deletion is intentionally NOT here yet — the memory store doesn't ship
a tested delete helper, so wiring one through a slash would mean either
a one-off ``Path.unlink()`` (risk of leaving the index inconsistent)
or a cross-layer change. Leaving the surface explicit so the right
addition is owner-directed (file an issue first).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import Session


_USAGE = (
    "Usage: /memory <list|view <name>>\n"
    "  list           — every memory entry (name, type, summary)\n"
    "  view <name>    — full body of a single entry"
)


def _memory_completer(
    session: "Session", arg_partial: str = "",
) -> list[str]:
    """Surface memory entry names after ``/memory view ``.

    Reads ``memory.list_entries()`` and returns the entry slugs. Empty
    list for ``/memory list`` or empty args (= hint mode covers those).
    """
    parts = arg_partial.split()
    sub = parts[0] if parts else ""
    if sub != "view":
        return []
    try:
        from reyn.data.memory import list_entries
        entries = list_entries()
        return [e.name for e in entries]
    except Exception:
        return []


@slash(
    "memory",
    summary="Inspect project memory entries",
    usage="/memory [list|view <name>]",
    completer=_memory_completer,
    see_also=("docs/concepts/data-retrieval/memory.md",),
)
async def memory_cmd(session: "Session", args: str) -> None:
    """Dispatch ``list`` / ``view <name>`` subcommands."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply(session, _USAGE)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        await _list_memory(session)
    elif sub == "view":
        await _view_memory(session, sub_args)
    else:
        await reply_error(session, _USAGE)


async def _list_memory(session: "Session") -> None:
    """Render every memory entry as one line per row.

    Sorted by name (the same order ``list_entries`` returns). Type +
    one-line description gives the reader a scannable index without
    having to open the side panel.
    """
    from reyn.data.memory import list_entries

    entries = list_entries()
    if not entries:
        await reply(
            session,
            'no memory entries yet — try: "remember <fact>"',
        )
        return
    # Column widths chosen to keep the line < 80 cells at typical
    # name / type lengths; long descriptions truncate with an ellipsis.
    name_w = max((len(e.name) for e in entries), default=8)
    type_w = max((len(e.type) for e in entries), default=8)
    lines = [f"memory entries ({len(entries)}):",
             f"  {'name':<{name_w}}  {'type':<{type_w}}  description"]
    for e in entries:
        desc = e.description or ""
        if len(desc) > 60:
            desc = desc[:59] + "…"
        lines.append(
            f"  {e.name:<{name_w}}  {e.type:<{type_w}}  {desc}"
        )
    await reply(session, "\n".join(lines))


async def _view_memory(session: "Session", name: str) -> None:
    """Print the full body of the named entry."""
    name = name.strip()
    if not name:
        await reply_error(session, "Usage: /memory view <name>")
        return
    from reyn.data.memory import find_one, list_entries

    try:
        entry = find_one(name)
    except Exception:
        # Fall back to exact-name match against the list so the user
        # gets a clean "not found" instead of a stacktrace-flavoured
        # error from the resolver.
        all_entries = list_entries()
        entry = next((e for e in all_entries if e.name == name), None)
    if entry is None:
        await reply_error(session, f"memory entry not found: {name!r}")
        return
    header = f"{entry.name}  [{entry.type}]"
    if entry.description:
        header += f"  — {entry.description}"
    body = entry.body or "(empty body)"
    await reply(session, f"{header}\n\n{body}")
