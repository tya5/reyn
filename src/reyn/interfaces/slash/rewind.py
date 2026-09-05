"""``/rewind`` — time-travel to an earlier checkpoint (ADR-0038 1f, ADR-0047).

Two forms:

- ``/rewind``      → opens the inline checkpoint picker (RewindMenuWidget). The
  handler emits a ``__rewind_menu__`` sentinel that ``app_outbox`` routes to
  the App, which reads ``AgentRegistry.list_rewind_points()`` and mounts the
  menu. This mirrors how ``/quit`` emits ``__quit__`` — the registry stays the
  single source of truth for slash dispatch while the App owns the TUI surface.

- ``/rewind <N> [global]``  → goes directly to WAL seq ``N`` via the unified
  ``AgentRegistry.checkout`` (scriptable + testable without the TUI) — undo for
  a live-branch seq, fork-switch for a dead-branch one; see the call site's own
  comment. Invalid / out-of-range targets surface a decision-enabling error.
  (This line used to name ``rewind_to``; the call became ``checkout`` under
  ADR-0038 D8 and the docstring had not followed.)

#5769 stage 3 ④ (ADR-0047 decision 3, owner ruling 2026-09-05 「規定はローカルがよいな」):
``/rewind`` now means two different operations, and the UI default is
**session-local** — rewinds only THIS session (``ctx.session``'s own
``(agent_name, session_id)``); the trailing word ``global`` opts into the
old whole-substrate cut explicitly. **The two layers answer differently ON
PURPOSE**: ``AgentRegistry.checkout``'s ``scope`` stays a required
keyword with no default (a caller that forgets it fails loudly rather than
silently drifting global) — this command layer is the one place that
decides what the user meant, and always states that decision to
``checkout`` explicitly. The UI's own default must never leak into
being the API's default.

Language note (lead-coder/architect correction, relayed to the owner):
never describe a rewound-past future as "gone" — it survives as an
INACTIVE branch (``list_rewind_points(include_abandoned=True)`` still
finds it; typing its seq again fork-switches back). The real asymmetries
a rewind carries: a cancelled in-flight turn's re-run spends budget again,
a target past the retention floor is genuinely unreachable, and a
``global`` rewind stops every OTHER session's in-flight work too — not
just this one's.
"""
from __future__ import annotations

from reyn.core.events.snapshot_generations import GLOBAL_SCOPE
from reyn.interfaces.slash import SlashContext, reply, reply_error, slash
from reyn.runtime.outbox import OutboxMessage


def _owner_marker(point: "dict", default_scope: "tuple[str, str]") -> str:
    """#5769 stage 3 ④: the same "is this row my own session's checkpoint"
    marker :func:`~reyn.interfaces.inline.textual_chat.rewind_picker.rewind_row_text`
    renders for the TUI picker, reimplemented here rather than imported —
    this module is always-loaded (every client, not just the TTY-only
    Textual one), and ``rewind_picker`` is TTY-only by its own module
    docstring; importing it here would cross that boundary.

    A point whose ``name``/``sid`` came back ``None`` (#5782 — an
    unresolved owner) is marked explicitly, never silently treated as
    this session's own. A point owned by a genuinely different session is
    named; a point matching ``default_scope`` carries no marker."""
    owner_name, owner_sid = point.get("name"), point.get("sid")
    if owner_name is None or owner_sid is None:
        return "  (owner unknown)"
    if (owner_name, owner_sid) != default_scope:
        return f"  ({owner_name}/{owner_sid})"
    return ""


@slash(
    "rewind",
    summary="Time-travel to an earlier checkpoint (no arg = pick from a menu)",
    locus="session",
    usage="/rewind [seq] [global]",
)
async def rewind_cmd(ctx: "SlashContext", args: str) -> None:
    arg = (args or "").strip()

    agent_name = getattr(ctx.session, "agent_name", None)
    session_id = getattr(ctx.session, "session_id", None)
    default_scope = (
        (agent_name, session_id) if agent_name is not None and session_id is not None else None
    )

    # Bare /rewind → open the checkpoint picker. F4: publish a command-UI request
    # the front-end renders (the inline CUI region as a selector, --cui as a text
    # list). Replaces a dead __rewind_menu__ sentinel that no inline handler
    # consumed (a silent no-op before this).
    if not arg:
        registry = getattr(ctx.session, "_registry", None)
        # #3987 ②: abandoned branches are offered too, so a fork the operator
        # left behind is reachable instead of merely existing. The rows keep
        # their natural (ascending-seq) order here — the tree builder does its
        # own DFS/active-first/newest-first ordering, and pre-reversing would
        # fight it. The flat fallback below reverses for itself.
        points = list(registry.list_rewind_points(include_abandoned=True)) if registry is not None else []
        branches = [
            {
                "branch_id": b.branch_id, "fork_point_seq": b.fork_point_seq,
                "head_seq": b.head_seq, "parent_branch_id": b.parent_branch_id,
                "is_active": b.is_active,
            }
            # ``list_branches`` returns ``Branch`` dataclasses; the tree builder
            # and the command-UI payload both speak plain dicts (the payload
            # crosses a transport boundary). Converted here, at the one seam.
            # #5789: `list_branches` is now SCOPED (decision table, #5786
            # review). `GLOBAL_SCOPE` here is a DELIBERATE, disclosed choice
            # preserving today's picker behavior byte-for-byte, not the
            # session-local default #5785 gave the actual rewind operation:
            # `points` above already aggregates EVERY session's own
            # checkpoints (each correctly tagged with its own owner, #5782),
            # so the tree must cover every branch those rows can reference —
            # a session-scoped tree would omit branches other sessions' rows
            # point at. Narrowing the PICKER itself to session-local-by-
            # default is a real UX decision (which forks a user sees without
            # asking) that needs the owner's own screen, not an inference
            # made here.
            for b in (
                registry.list_branches(scope=GLOBAL_SCOPE) if registry is not None else []
            )
        ]
        if not points:
            await reply(ctx, "/rewind: no earlier checkpoints to rewind to")
            return
        # Inline CUI: the region polls this and shows a ↑↓ selector.
        # #5769 stage 3 ④: `default_scope` travels alongside the points so
        # the front-end can state, BEFORE the operator picks a row, which
        # of the two rewind shapes picking a row will perform (ADR-0047
        # decision 3) — visible ahead of the operation, not only in the
        # after-the-fact summary reply.
        ctx.session.set_pending_command_ui({
            "kind": "rewind", "points": points, "branches": branches,
            "default_scope": (
                {"agent": agent_name, "sid": session_id} if default_scope is not None else None
            ),
        })
        # --cui fallback: a text list (the output loop renders this only on the
        # plain path; the inline path skips it since the region shows a selector).
        header = "rewind to a checkpoint with /rewind <seq>"
        header += (
            f" — default: session-local ({agent_name}/{session_id}); "
            "add 'global' to rewind every session instead"
            if default_scope is not None
            else " (add 'global' after the seq to rewind every session)"
        )
        lines = [header + ":"]
        # #5648: the anchor (#1547, already truncated upstream by
        # AnchorStore.truncate_anchor — never re-truncated here) — same 4th
        # column the TUI picker's own rewind_row_text renders, so this
        # fallback list tells --connect the same "where in the conversation"
        # hint (owner-hit: a candidate with only seq/kind gave no clue which
        # checkpoint to pick).
        # #3987 ②: this fallback now receives abandoned checkpoints too, so it
        # MARKS them. Without the marker the extra rows would look like ordinary
        # candidates on a --connect client — new rows, no way to tell they sit
        # on a fork the operator left. The TUI picker shows the same fact
        # structurally (branch headers + indent); this surface has one column,
        # so it says it in words.
        _active_ids = {b["branch_id"] for b in branches if b["is_active"]}
        lines += [
            f"  seq {p.get('seq')} · {p.get('kind', '?')}"
            + (f" · 「{p['anchor']}」" if p.get("anchor") else "")
            + ("" if p.get("branch_id") in _active_ids else "  (abandoned)")
            # #5769 stage 3 ④: owner marker — never treats a #5782
            # unresolved (None, None) owner as if it were "main"/this
            # session's own.
            + (_owner_marker(p, default_scope) if default_scope is not None else "")
            for p in reversed(points)
        ]
        ctx.transport.put_display(
            OutboxMessage(kind="__rewind_list__", text="\n".join(lines))
        )
        return

    # /rewind <N> [global] → direct rewind to WAL seq N.
    parts = arg.split()
    global_requested = len(parts) == 2 and parts[1].lower() == "global"
    if not (len(parts) == 1 or global_requested):
        await reply_error(ctx, "usage: /rewind <seq> [global]")
        return
    try:
        target = int(parts[0])
    except ValueError:
        await reply_error(ctx, f"/rewind: expected a checkpoint seq (integer), got {parts[0]!r}")
        return

    registry = getattr(ctx.session, "_registry", None)
    if registry is None:
        await reply_error(ctx, "/rewind: no agent registry attached (rewind unavailable)")
        return

    # #5769 stage 3 ④ (ADR-0047 decision 3): the command layer decides the
    # user's intent HERE and states it explicitly to checkout() — which
    # never defaults scope on its own (a required keyword, ADR-0047
    # decision 2/3). The UI's own default (session-local) must never
    # become the API's default; it is chosen and passed every time.
    if global_requested:
        # #5769/#5784: GLOBAL_SCOPE is the explicit spelling for "this call
        # site's scope is genuinely, permanently global" -- a bare `None`
        # literal here would carry the same value but not the same fact
        # (see GLOBAL_SCOPE's own docstring).
        scope = GLOBAL_SCOPE
        scope_label = "GLOBAL (every session)"
    elif default_scope is not None:
        scope = default_scope
        scope_label = f"session-local ({default_scope[0]}/{default_scope[1]})"
    else:
        await reply_error(
            ctx,
            "/rewind: could not determine this session's own identity for a "
            "session-local rewind — add 'global' to rewind every session instead",
        )
        return

    # Visible BEFORE the operation (architect scope, #5769 stage 3 ④):
    # state which of the two shapes this is before calling checkout, not
    # only after. See this module's own docstring for why "gone"/"lost"
    # language is wrong here — the target stays reachable as an inactive
    # branch either way.
    await reply(ctx, f"⏪ rewinding {scope_label} to seq {target} …")

    try:
        # Unified checkout (ADR-0038 D8): the same op the picker dispatches —
        # undo for a live-branch seq, fork-switch for a dead-branch seq. Keeps
        # the two "go to seq N" entries (slash + picker) behaviourally identical
        # (no sibling-gap); checkout subsumes rewind_to for active seqs.
        result = await registry.checkout(target, scope=scope)
    except Exception as exc:  # noqa: BLE001 — surface the reason to the user
        await reply_error(ctx, f"/rewind: {exc}")
        return

    agents = result.get("agents", [])
    # #2115: report the ACTUAL in-flight disposition (cancelled vs
    # finished-before-the-cancel-landed) — not a hardcoded "cancelled" literal.
    summary = (
        f"⏪ checked out to seq {result.get('target_n', target)} "
        f"· {len(agents)} agent(s) reset"
    )
    cancelled = result.get("in_flight_cancelled", 0)
    finished = result.get("in_flight_finished", 0)
    bits = []
    if cancelled:
        bits.append(f"{cancelled} in-flight cancelled")
    if finished:
        bits.append(f"{finished} in-flight finished")
    if bits:
        summary += " · " + ", ".join(bits)
    await reply(ctx, summary)


__all__ = ["rewind_cmd"]
