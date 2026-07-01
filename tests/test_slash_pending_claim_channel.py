"""Tier 2: /pending claim must pass DEFAULT_CHAT_CHANNEL_ID to claim_pending_intervention.

After the Textual TUI removal, the REPL listener is registered as
DEFAULT_CHAT_CHANNEL_ID ("tui").  The old code hardcoded f"tui:{agent_name}"
(the Textual TUI's per-agent naming convention).

InterventionCoordinator.dispatch checks has_listener(iv.origin_channel_id) and
parks the iv as stalled when it returns False.  Passing "tui:default" when the
only registered listener is "tui" caused the iv to be immediately re-parked stalled
after a claim — the user saw "claimed ..." but the intervention never reappeared.

Falsification: if _claim passed f"tui:{agent_name}" the assertion
    recorded_channel_id == "tui"
would fail with "tui:default", directly exposing the regression.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.pending import _claim
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID


class _ClaimStubSession:
    """Minimal session stub that records the channel_id passed to claim."""

    def __init__(self, *, agent_name: str = "default") -> None:
        self.agent_name = agent_name
        self.outbox_messages: list[OutboxMessage] = []
        self.recorded_channel_id: str | None = None

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def list_stalled_interventions(self) -> list:
        from types import SimpleNamespace
        return [SimpleNamespace(id="iv-abc12345", kind="ask_user", summary="Allow?")]

    async def claim_pending_intervention(self, iv_id: str, channel_id: str):
        self.recorded_channel_id = channel_id
        from types import SimpleNamespace
        return SimpleNamespace(summary="Allow?")


@pytest.mark.asyncio
async def test_claim_passes_default_chat_channel_id() -> None:
    """Tier 2: _claim passes DEFAULT_CHAT_CHANNEL_ID not f"tui:{agent_name}".

    Regression guard: old code used f"tui:{agent_name}" (Textual TUI convention),
    which mismatches the registered REPL listener "tui" →
    InterventionCoordinator.dispatch immediately re-parks the iv stalled.
    """
    sess = _ClaimStubSession(agent_name="default")
    await _claim(sess, "iv-abc12345")

    assert sess.recorded_channel_id == DEFAULT_CHAT_CHANNEL_ID, (
        f"_claim must pass DEFAULT_CHAT_CHANNEL_ID={DEFAULT_CHAT_CHANNEL_ID!r} "
        f"not the old Textual TUI-style 'tui:<agent>'; "
        f"got {sess.recorded_channel_id!r}"
    )


@pytest.mark.asyncio
async def test_claim_channel_is_not_agent_namespaced() -> None:
    """Tier 2: claimed channel must NOT be f"tui:{agent_name}".

    The old value "tui:default" has no registered listener in the current REPL
    (listener = "tui"), causing the iv to be re-parked stalled on re-dispatch.
    """
    sess = _ClaimStubSession(agent_name="myagent")
    await _claim(sess, "iv-abc12345")

    # Must not be the old Textual-TUI agent-namespaced form.
    assert sess.recorded_channel_id != f"tui:{sess.agent_name}", (
        "claimed channel must not use agent-namespaced form 'tui:<agent>'; "
        "use DEFAULT_CHAT_CHANNEL_ID instead"
    )
