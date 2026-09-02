"""``/rewind`` — time-travel to an earlier checkpoint (ADR-0038 1f).

Two forms:

- ``/rewind``      → opens the inline checkpoint picker (RewindMenuWidget). The
  handler emits a ``__rewind_menu__`` sentinel that ``app_outbox`` routes to
  the App, which reads ``AgentRegistry.list_rewind_points()`` and mounts the
  menu. This mirrors how ``/quit`` emits ``__quit__`` — the registry stays the
  single source of truth for slash dispatch while the App owns the TUI surface.

- ``/rewind <N>``  → goes directly to WAL seq ``N`` via the unified
  ``AgentRegistry.checkout`` (scriptable + testable without the TUI) — undo for
  a live-branch seq, fork-switch for a dead-branch one; see the call site's own
  comment. Invalid / out-of-range targets surface a decision-enabling error.
  (This line used to name ``rewind_to``; the call became ``checkout`` under
  ADR-0038 D8 and the docstring had not followed.)
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash
from reyn.runtime.outbox import OutboxMessage


@slash(
    "rewind",
    summary="Time-travel to an earlier checkpoint (no arg = pick from a menu)",
    locus="session",
    usage="/rewind [seq]",
)
async def rewind_cmd(ctx: "SlashContext", args: str) -> None:
    arg = (args or "").strip()

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
            for b in (registry.list_branches() if registry is not None else [])
        ]
        if not points:
            await reply(ctx, "/rewind: no earlier checkpoints to rewind to")
            return
        # Inline CUI: the region polls this and shows a ↑↓ selector.
        ctx.session.set_pending_command_ui(
            {"kind": "rewind", "points": points, "branches": branches},
        )
        # --cui fallback: a text list (the output loop renders this only on the
        # plain path; the inline path skips it since the region shows a selector).
        lines = ["rewind to a checkpoint with /rewind <seq>:"]
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
            for p in reversed(points)
        ]
        ctx.transport.put_display(
            OutboxMessage(kind="__rewind_list__", text="\n".join(lines))
        )
        return

    # /rewind <N> → direct rewind to WAL seq N.
    try:
        target = int(arg)
    except ValueError:
        await reply_error(ctx, f"/rewind: expected a checkpoint seq (integer), got {arg!r}")
        return

    registry = getattr(ctx.session, "_registry", None)
    if registry is None:
        await reply_error(ctx, "/rewind: no agent registry attached (rewind unavailable)")
        return

    try:
        # Unified checkout (ADR-0038 D8): the same op the picker dispatches —
        # undo for a live-branch seq, fork-switch for a dead-branch seq. Keeps
        # the two "go to seq N" entries (slash + picker) behaviourally identical
        # (no sibling-gap); checkout subsumes rewind_to for active seqs.
        result = await registry.checkout(target)
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
