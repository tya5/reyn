"""``/clear-history`` — wipe chat history.

Sibling to ``/reset`` (run state) at a different scope: this command
clears the conversation thread (``Session.history`` + per-agent
``history/`` segment directory). Everything else stays intact:

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
delete is irreversible (= the ``history/`` segment directory isn't
tracked by git in any typical project layout).

#6240/#6248 (architect ruling on PR #6257's own review, issuecomment-
5773552909): the ACTUAL disk wipe + the handle-close/reopen ordering
invariant it depends on now live on :meth:`Session.clear_history`, not
here. This handler was ALREADY reaching across that boundary before
segments existed (``history_path.unlink()`` / ``history.clear()``), just
through PUBLIC attribute names that #3595 S4's residue gate had no way
to see — #6248 did not introduce that crossing, only exposed it, once
the operation grew a real ordering invariant (a held-open file handle) a
slash module cannot safely honor from the outside. This handler keeps
only the confirm-flow UX: the two-step confirmation prompt, the
``Currently: N turns`` line, and the success/error reply text — see
``Session.clear_history``'s own docstring for the 4-step order and why
it is load-bearing.
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
    clear_op = getattr(ctx.session, "clear_history", None)

    if not callable(clear_op):
        # A session-shaped stub with no real disk-backed history at all
        # (test doubles, or a future session kind) — nothing to wipe.
        await reply(ctx, "✓ Nothing to clear (= no history).")
        return

    try:
        n_turns_before = clear_op()
    except OSError as exc:
        history_dir = getattr(ctx.session, "history_dir", None)
        await reply_error(
            ctx,
            f"failed to remove history directory {history_dir}: {exc}",
        )
        return

    if not n_turns_before and not isinstance(history, list):
        await reply(ctx, "✓ Nothing to clear (= no history).")
        return

    await reply(
        ctx,
        f"✓ Cleared: {n_turns_before} history turn(s). "
        "Audit logs and run state preserved.",
    )
