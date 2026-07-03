"""``/rewind`` — time-travel to an earlier checkpoint (ADR-0038 1f).

Two forms:

- ``/rewind``      → opens the inline checkpoint picker (RewindMenuWidget). The
  handler emits a ``__rewind_menu__`` sentinel that ``app_outbox`` routes to
  the App, which reads ``AgentRegistry.list_rewind_points()`` and mounts the
  menu. This mirrors how ``/quit`` emits ``__quit__`` — the registry stays the
  single source of truth for slash dispatch while the App owns the TUI surface.

- ``/rewind <N>``  → rewinds directly to WAL seq ``N`` via
  ``AgentRegistry.rewind_to`` (scriptable + testable without the TUI). Invalid
  / out-of-range targets surface a decision-enabling error.
"""
from __future__ import annotations

from reyn.interfaces.slash import reply, reply_error, slash
from reyn.runtime.outbox import OutboxMessage


@slash(
    "rewind",
    summary="Time-travel to an earlier checkpoint (no arg = pick from a menu)",
    usage="/rewind [seq]",
)
async def rewind_cmd(session: "object", args: str) -> None:
    arg = (args or "").strip()

    # Bare /rewind → open the checkpoint picker. F4: publish a command-UI request
    # the front-end renders (the inline CUI region as a selector, --cui as a text
    # list). Replaces a dead __rewind_menu__ sentinel that no inline handler
    # consumed (a silent no-op before this).
    if not arg:
        registry = getattr(session, "_registry", None)
        points = list(reversed(registry.list_rewind_points())) if registry is not None else []
        if not points:
            await reply(session, "/rewind: no earlier checkpoints to rewind to")
            return
        # Inline CUI: the region polls this and shows a ↑↓ selector.
        session.set_pending_command_ui({"kind": "rewind", "points": points})
        # --cui fallback: a text list (the output loop renders this only on the
        # plain path; the inline path skips it since the region shows a selector).
        lines = ["rewind to a checkpoint with /rewind <seq>:"]
        lines += [f"  seq {p.get('seq')} · {p.get('kind', '?')}" for p in points]
        await session._put_outbox(
            OutboxMessage(kind="__rewind_list__", text="\n".join(lines))
        )
        return

    # /rewind <N> → direct rewind to WAL seq N.
    try:
        target = int(arg)
    except ValueError:
        await reply_error(session, f"/rewind: expected a checkpoint seq (integer), got {arg!r}")
        return

    registry = getattr(session, "_registry", None)
    if registry is None:
        await reply_error(session, "/rewind: no agent registry attached (rewind unavailable)")
        return

    try:
        # Unified checkout (ADR-0038 D8): the same op the picker dispatches —
        # undo for a live-branch seq, fork-switch for a dead-branch seq. Keeps
        # the two "go to seq N" entries (slash + picker) behaviourally identical
        # (no sibling-gap); checkout subsumes rewind_to for active seqs.
        result = await registry.checkout(target)
    except Exception as exc:  # noqa: BLE001 — surface the reason to the user
        await reply_error(session, f"/rewind: {exc}")
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
    await reply(session, summary)


__all__ = ["rewind_cmd"]
