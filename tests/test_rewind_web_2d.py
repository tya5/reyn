"""Tier 2: 2d web surface — `/rewind <N>` checkout via the Chainlit slash path.

The Chainlit glue (``app.py:787`` — ``if is_slash(text): session._maybe_handle_slash``)
routes ALL slashes to the same dispatcher the TUI uses, so ``/rewind <N>`` reaches
``slash/rewind.py`` → ``registry.checkout(N)`` with no web-specific code. This pins
that path with a **non-default-seq round-trip** (per
feedback_roundtrip_test_nondefault_value): checkout to an EARLIER seq (not the
head, not a no-op) and verify the active branch actually moved there — so an
unwired / no-op checkout can't silent-pass.

(The bare-`/rewind` tree picker for web is 2d-2 — a Chainlit cl.Action surface,
separate.) Real AgentRegistry + real slash handler — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.snapshot_generations import is_active_seq
from reyn.core.events.state_log import StateLog
from reyn.interfaces.chainlit_app.slash_route import is_slash
from reyn.interfaces.slash import REGISTRY


class _CapturingSession:
    def __init__(self, registry) -> None:
        self.agent_name = "alpha"
        self._registry = registry
        self.outbox_msgs: list = []

    async def _put_outbox(self, msg) -> None:
        self.outbox_msgs.append(msg)


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


def test_chainlit_recognises_rewind_as_slash() -> None:
    """Tier 2: the Chainlit is_slash gate (app.py:787) routes /rewind to the
    shared dispatcher — so /rewind <N> reaches the same handler as the TUI."""
    assert is_slash("/rewind 5") is True
    assert is_slash("/rewind") is True
    assert is_slash("not a slash") is False


@pytest.mark.asyncio
async def test_rewind_seq_checkout_round_trip_nondefault(tmp_path) -> None:
    """Tier 2: /rewind <N> (Chainlit path) checks out to a NON-DEFAULT earlier
    seq — the active branch moves to N and the later seq is abandoned. A no-op /
    unwired checkout would leave the head active and FAIL this.
    """
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")  # head
    assert is_active_seq(log, s1) and is_active_seq(log, s2)   # both active pre-rewind

    # Drive the slash handler exactly as the Chainlit glue does
    # (is_slash → session._maybe_handle_slash → REGISTRY → rewind handler).
    session = _CapturingSession(registry=reg)
    await REGISTRY.get("rewind").handler(session, str(s1))   # /rewind <s1>, non-default

    # Round-trip: the active branch moved to s1; s2 (the former head) is abandoned.
    assert is_active_seq(log, s1) is True
    assert is_active_seq(log, s2) is False
    texts = [getattr(m, "text", "") for m in session.outbox_msgs]
    assert any("checked out" in t and f"seq {s1}" in t for t in texts)
