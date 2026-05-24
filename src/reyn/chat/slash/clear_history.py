"""``/clear-history`` — wipe chat history + action-usage table.

Sibling to ``/reset`` (skill state) at a different scope: this command
clears the conversation thread (``ChatSession.history`` + per-agent
``history.jsonl``) and the action-usage tracker (= the ranking that
backs the Memory tab's hot-list augmentation, persisted at
``.reyn/state/action_usage.jsonl``). Everything else stays intact:

- ``.reyn/events/``                (P6 audit truth — never touched)
- ``.reyn/state/wal.jsonl``        (skill resume — preserved)
- ``.reyn/agents/<n>/state/``      (snapshot.json / plans / skills)
- ``profile.yaml`` / MEMORY.md     (non-runtime config)
- ``.input_history``               (operator's typed history)

User dogfood 2026-05-25:
  「ヒストリとagents_usage を初期状態にする、 他はクリアしない」

Two-step confirmation pattern mirrors ``/reset`` because the history
delete is irreversible (= history.jsonl isn't tracked by git in any
typical project layout).
"""
from __future__ import annotations

from reyn.chat.slash import reply, reply_error, slash


def _format_currently_line(session: "object") -> str:
    """Build a 'Currently: N history turns, M tracked tools' context line."""
    history = getattr(session, "history", None)
    tracker = getattr(session, "_action_usage_tracker", None)

    parts: list[str] = []
    if history is not None:
        n_turns = len(history)
        word = "turn" if n_turns == 1 else "turns"
        parts.append(f"{n_turns} history {word}")
    if tracker is not None:
        try:
            n_tools = len(tracker)
        except TypeError:
            n_tools = 0
        word = "tool" if n_tools == 1 else "tools"
        parts.append(f"{n_tools} tracked {word}")
    if not parts:
        return ""
    return "Currently: " + ", ".join(parts) + "."


@slash(
    "clear-history",
    summary=(
        "Clear conversation history + action-usage table (= events, "
        "skill state, profile preserved)"
    ),
    usage="/clear-history confirm",
)
async def clear_history_cmd(session: "object", args: str) -> None:
    token = args.strip().lower()
    if token != "confirm":
        currently = _format_currently_line(session)
        preamble = f"{currently}\n" if currently else ""
        await reply(
            session,
            f"{preamble}"
            "⚠ This will clear the chat history and the action-usage "
            "ranking. Audit logs (.reyn/events/), in-flight skill state "
            "(WAL + snapshots), agent profile, and MEMORY.md are all "
            "preserved.\n"
            "Type `/clear-history confirm` to proceed, or anything else "
            "to abort.",
        )
        return

    history = getattr(session, "history", None)
    history_path = getattr(session, "history_path", None)
    tracker = getattr(session, "_action_usage_tracker", None)

    cleared_parts: list[str] = []

    if isinstance(history, list):
        n_turns_before = len(history)
        history.clear()
        cleared_parts.append(f"{n_turns_before} history turn(s)")

    if history_path is not None:
        try:
            history_path.unlink(missing_ok=True)
        except OSError as exc:
            await reply_error(
                session,
                f"failed to remove history file {history_path}: {exc}",
            )
            return

    if tracker is not None and hasattr(tracker, "reset"):
        try:
            n_tools_before = len(tracker)
        except TypeError:
            n_tools_before = 0
        tracker.reset()
        cleared_parts.append(f"{n_tools_before} tracked tool(s)")

    if not cleared_parts:
        await reply(
            session,
            "✓ Nothing to clear (= history empty, no action-usage tracker).",
        )
        return

    await reply(
        session,
        "✓ Cleared: " + ", ".join(cleared_parts) + ". "
        "Audit logs and skill state preserved.",
    )
