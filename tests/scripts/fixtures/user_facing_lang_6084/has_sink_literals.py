"""Fixture for user_facing_lang_gate.py's own test suite (#6084).

Not a real module — never imported, never collected as a test.
`tests/scripts/test_user_facing_lang_gate_6084.py` reads this file
directly by path (parsed, never executed). Carries exactly THREE flagged
sink call sites: an `OutboxMessage(text=...)` literal, a `reply(ctx, ...)`
positional literal, and a `DrawerRow(label=...)` literal.
"""
from __future__ import annotations


async def reply(ctx, text: str) -> None:  # stand-in for the real slash-reply helper
    ...


class DrawerRow:  # stand-in for the real custom widget
    def __init__(self, *, label: str, command=None) -> None: ...


async def emit(ctx) -> None:
    from reyn.runtime.outbox import OutboxMessage

    ctx.transport.put_display(OutboxMessage(kind="system", text="こんにちは"))
    await reply(ctx, "ダメです")


def rows():
    return [DrawerRow(label="実行中の task はありません", command=None)]
