"""Tier 2: _simple_status builds a real status OutboxMessage.

It is called from the REPL input loop's "no agent attached" path, so a broken
import there crashes the whole REPL the moment the user types. This pins that
_simple_status actually constructs an OutboxMessage (a wrong import module would
raise ModuleNotFoundError when the function runs, not at module load).
"""
from __future__ import annotations

from reyn.interfaces.repl.stream_client import _simple_status
from reyn.runtime.outbox import OutboxMessage


def test_simple_status_returns_status_outbox_message() -> None:
    """Tier 2: returns an OutboxMessage of kind 'status' carrying the text."""
    msg = _simple_status("no agent attached; try :agents")
    assert isinstance(msg, OutboxMessage)
    assert msg.kind == "status"
    assert msg.text == "no agent attached; try :agents"
