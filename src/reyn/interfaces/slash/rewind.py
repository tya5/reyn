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

from reyn.chat.outbox import OutboxMessage
from reyn.interfaces.slash import reply, reply_error, slash


@slash(
    "rewind",
    summary="Time-travel to an earlier checkpoint (no arg = pick from a menu)",
    usage="/rewind [seq]",
)
async def rewind_cmd(session: "object", args: str) -> None:
    arg = (args or "").strip()

    # Bare /rewind → open the picker via the TUI (sentinel routed by app_outbox).
    if not arg:
        await session._put_outbox(OutboxMessage(kind="__rewind_menu__", text=""))
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
    await reply(
        session,
        f"⏪ checked out to seq {result.get('target_n', target)} "
        f"· {len(agents)} agent(s) reset · in-flight cancelled",
    )


__all__ = ["rewind_cmd"]
