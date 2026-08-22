"""/memory slash command — inspect the project memory store from chat.

Read-only subcommands:

  /memory list                 — list every memory entry (name + type + summary)
  /memory view <name>          — print the full body of a single entry

Deletion is intentionally NOT here yet — the memory store doesn't ship
a tested delete helper, so wiring one through a slash would mean either
a one-off ``Path.unlink()`` (risk of leaving the index inconsistent)
or a cross-layer change. Leaving the surface explicit so the right
addition is owner-directed (file an issue first).

#3721: ``list_entries()`` / ``find_one()`` used to be called with no
directory argument, which falls back to ``memory_dir()``'s ambient
``Path.cwd()``-relative default — the SAME incident class as #3705/#3716,
just on the read side (a wrong-project read, not a write into the wrong
project). The fix resolves the project root through
``ctx.transport.reyn_state_root()`` (#3721's new seam on ``ClientTransport``)
rather than ``ctx.session`` directly, per #3595 S4's ratchet: a NEW private
read off the session residue field would grow exactly what that gate is
closing. ``None`` means "not resolvable through this connection" (a
genuinely remote transport) — surfaced as its own message, never silently
folded into "0 memories found" (lead-coder's fix condition on #3721: those
are different answers to different questions).
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash

_ROOT_UNRESOLVED = (
    "can't determine this project's memory location over this connection "
    "(no local project root to resolve against) — memory lookup isn't "
    "available here yet."
)


_USAGE = (
    "Usage: /memory <list|view <name>>\n"
    "  list           — every memory entry (name, type, summary)\n"
    "  view <name>    — full body of a single entry"
)


def _memory_completer(
    source: "object", arg_partial: str = "",
) -> list[str]:
    """Surface memory entry names after ``/memory view ``.

    Reads ``memory.list_entries()`` and returns the entry slugs. Empty
    list for ``/memory list`` or empty args (= hint mode covers those).

    ``source`` is a ``CompletionSourceSnapshot | None`` (#5044) — a plain
    value, never a live ``Session``. Only
    :attr:`~reyn.interfaces.repl.read_model.CompletionSourceSnapshot.
    workspace_dir` is needed: the completer still does its OWN disk I/O
    keyed by that Path, exactly as before — a static value, not a live
    method, so nothing here crosses a thread boundary any differently
    than reading a plain field.
    """
    parts = arg_partial.split()
    sub = parts[0] if parts else ""
    if sub != "view":
        return []
    workspace_dir = getattr(source, "workspace_dir", None)
    if workspace_dir is None:
        return []
    try:
        from reyn.data.memory import list_entries, memory_dir
        root = workspace_dir.parent.parent
        entries = list_entries(memory_dir(root=root))
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
async def memory_cmd(ctx: "SlashContext", args: str) -> None:
    """Dispatch ``list`` / ``view <name>`` subcommands."""
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await reply(ctx, _USAGE)
        return
    sub = parts[0]
    sub_args = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        await _list_memory(ctx)
    elif sub == "view":
        await _view_memory(ctx, sub_args)
    else:
        await reply_error(ctx, _USAGE)


async def _list_memory(ctx: "SlashContext") -> None:
    """Render every memory entry as one line per row.

    Sorted by name (the same order ``list_entries`` returns). Type +
    one-line description gives the reader a scannable index without
    having to open the side panel.
    """
    from reyn.data.memory import list_entries, memory_dir

    root = ctx.transport.reyn_state_root()
    if root is None:
        await reply_error(ctx, _ROOT_UNRESOLVED)
        return
    entries = list_entries(memory_dir(root=root))
    if not entries:
        await reply(
            ctx,
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
    await reply(ctx, "\n".join(lines))


async def _view_memory(ctx: "SlashContext", name: str) -> None:
    """Print the full body of the named entry."""
    name = name.strip()
    if not name:
        await reply_error(ctx, "Usage: /memory view <name>")
        return
    from reyn.data.memory import AmbiguousMemoryError, find_one, list_entries, memory_dir

    root = ctx.transport.reyn_state_root()
    if root is None:
        await reply_error(ctx, _ROOT_UNRESOLVED)
        return
    entries = list_entries(memory_dir(root=root))
    try:
        entry = find_one(name, entries)
    except AmbiguousMemoryError as exc:
        matches = ", ".join(e.slug for e in exc.matches)
        await reply_error(
            ctx, f"{name!r} matches multiple entries ({matches}) — pass the exact slug.",
        )
        return
    if entry is None:
        await reply_error(ctx, f"memory entry not found: {name!r}")
        return
    header = f"{entry.name}  [{entry.type}]"
    if entry.description:
        header += f"  — {entry.description}"
    body = entry.body or "(empty body)"
    await reply(ctx, f"{header}\n\n{body}")
