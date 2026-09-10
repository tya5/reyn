"""Fixture for user_facing_lang_gate.py's own test suite (#6084).

Not a real module — never imported, never collected as a test. Every kana
string here reaches a sink through indirection (dict lookup, another
function's return value) — this IS the out-of-scope witness: ruling 1
puts argument-side indirection out of scope, and this fixture's sink call
sites must produce ZERO findings despite carrying kana strings nearby.
"""
from __future__ import annotations

_LABELS = {"empty": "実行中の task はありません"}


def unavailable_message() -> str:
    return "ダメです"


async def reply(ctx, text: str) -> None:  # stand-in for the real slash-reply helper
    ...


async def emit(ctx) -> None:
    from reyn.runtime.outbox import OutboxMessage

    # Sink literal argument is a dict lookup, not a literal at the call
    # site — out of scope (same shape as compaction_progress.py:154/158
    # from #6084's investigation comment).
    ctx.transport.put_display(OutboxMessage(kind="system", text=_LABELS["empty"]))
    # Sink literal argument is a function call's return value — out of
    # scope (same shape as voice.py:83-91).
    await reply(ctx, unavailable_message())
