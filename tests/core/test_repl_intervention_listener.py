"""Tier 2: the REPL must register an intervention listener so ask_user surfaces.

A chat Session is built with enforce_listener_presence=True, so with NO listener
registered every intervention (ask_user / cost-warn confirm / permission prompt)
short-circuits to an empty answer — a silent auto-refuse. run_repl registers
DEFAULT_CHAT_CHANNEL_ID on the attached session to restore the listener the
Textual TUI registered before the inline cutover dropped it. This pins the
contract the fix relies on: enforcement is on, and registering that channel id
flips has_active_listener so interventions surface instead of auto-refusing.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from tests._support.agent_session import make_session


def test_chat_session_auto_refuses_until_repl_listener_is_registered(
    tmp_path: Path,
) -> None:
    """Tier 2: a chat session enforces listener presence (no listener → auto-
    refuse); registering the REPL's channel id makes interventions surface."""
    session = make_session(
        agent_name="default", state_log=StateLog(tmp_path / "wal.jsonl")
    )
    # Production config: fail-closed when nothing is listening.
    assert session.interventions.is_listener_enforcement_enabled()
    # The regression: with no listener wired, every intervention auto-refuses.
    assert not session.interventions.has_active_listener()
    # What run_repl now does on the attached session.
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    assert session.interventions.has_active_listener()
