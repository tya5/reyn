"""``/clear-history`` — wipe chat history.

Sibling to ``/reset`` (run state) at a different scope: this command
clears the conversation thread (``Session.history`` + per-agent
``history.jsonl``). Everything else stays intact:

- ``.reyn/events/``                (P6 audit truth — never touched)
- ``.reyn/state/wal.jsonl``        (run resume — preserved)
- ``.reyn/agents/<n>/state/``      (snapshot.json / plans)
- ``profile.yaml`` / MEMORY.md     (non-runtime config)
- ``.input_history``               (operator's typed history)

User dogfood 2026-05-25:
  「ヒストリとagents_usage を初期状態にする、 他はクリアしない」

#4552: this command used to also clear the action-usage tracker (the
freq+recency ranking that backed the Memory tab's hot-list augmentation,
persisted at ``.reyn/agents/<name>/action_usage.json``) — removed with
the hot-list feature it existed for (owner directive: discarded,
superseded by ``list_actions`` as the canonical discovery path). Only
the history half of the original user request survives; there is no
longer an action-usage table to clear.

Two-step confirmation pattern mirrors ``/reset`` because the history
delete is irreversible (= history.jsonl isn't tracked by git in any
typical project layout).
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


def _format_currently_line(session: "object") -> str:
    """Build a 'Currently: N history turns' context line."""
    history = getattr(session, "history", None)
    if history is None:
        return ""
    n_turns = len(history)
    word = "turn" if n_turns == 1 else "turns"
    return f"Currently: {n_turns} history {word}."


@slash(
    "clear-history",
    aliases=("clear",),
    summary=(
        "Clear conversation history (= events, run state, profile preserved)"
    ),
    locus="session",
    usage="/clear-history confirm",
)
async def clear_history_cmd(ctx: "SlashContext", args: str) -> None:
    token = args.strip().lower()
    if token != "confirm":
        currently = _format_currently_line(ctx.session)
        preamble = f"{currently}\n" if currently else ""
        await reply(
            ctx,
            f"{preamble}"
            "⚠ This will clear the chat history. Audit logs "
            "(.reyn/events/), in-flight run state (WAL + snapshots), "
            "agent profile, and MEMORY.md are all preserved.\n"
            "Type `/clear-history confirm` to proceed, or anything else "
            "to abort.",
        )
        return

    history = getattr(ctx.session, "history", None)
    history_path = getattr(ctx.session, "history_path", None)

    # Snapshot size before any mutation so the report is accurate even if
    # disk deletion is attempted first.
    n_turns_before = len(history) if isinstance(history, list) else 0

    # Disk deletion first: if it fails the in-memory state is unchanged and
    # the next session restart will see a consistent (uncorrupted) history.
    # Clearing memory first then failing on disk leaves the opposite: the
    # current session sees empty history but history.jsonl survives and
    # reloads the old turns on next startup.
    if history_path is not None:
        try:
            history_path.unlink(missing_ok=True)
        except OSError as exc:
            await reply_error(
                ctx,
                f"failed to remove history file {history_path}: {exc}",
            )
            return

    if not isinstance(history, list):
        await reply(ctx, "✓ Nothing to clear (= no history).")
        return

    history.clear()
    await reply(
        ctx,
        f"✓ Cleared: {n_turns_before} history turn(s). "
        "Audit logs and run state preserved.",
    )
