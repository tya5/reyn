"""Tier 2: /rewind slash command — time-travel dispatch (ADR-0038 1f).

Two forms:
- ``/rewind``     → emits the ``__rewind_menu__`` sentinel (app_outbox routes
  it to the App, which opens the inline picker). Mirrors ``/quit``→``__quit__``.
- ``/rewind <N>`` → calls ``AgentRegistry.rewind_to(N)`` directly and surfaces
  the result (scriptable + TUI-free).

Real AgentRegistry + StateLog for the ``<N>`` path (no mocks); a light session
stub captures the outbox for the sentinel + error paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


class _CapturingSession:
    """Minimal session: captures outbox messages, holds an optional registry."""

    def __init__(self, registry=None) -> None:
        self.agent_name = "test"
        self._registry = registry
        self.outbox_msgs: list = []
        self._pending_command_ui = None

    async def _put_outbox(self, msg) -> None:
        self.outbox_msgs.append(msg)

    @property
    def pending_command_ui(self):
        return self._pending_command_ui

    def set_pending_command_ui(self, payload) -> None:
        self._pending_command_ui = payload


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


def _handler():
    cmd = REGISTRY.get("rewind")
    assert cmd is not None
    return cmd


def test_rewind_is_registered() -> None:
    """Tier 2: /rewind is in the registry with the seq usage hint."""
    cmd = _handler()
    assert "seq" in cmd.usage.lower()


class _StubRewindRegistry:
    # list_rewind_points() returns ascending seq (oldest first), mirroring the real impl.
    def list_rewind_points(self, **_kw):
        return [{"seq": 38, "kind": "turn"}, {"seq": 42, "kind": "turn"}]


@pytest.mark.asyncio
async def test_bare_rewind_opens_picker_via_command_ui_and_text_fallback() -> None:
    """Tier 2: bare /rewind (F4) publishes a command-UI request (the inline region
    selector) AND a __rewind_list__ text fallback (the --cui path).

    The slash handler reverses list_rewind_points() so the picker shows most-recent
    checkpoints first (seq 42 before seq 38 when 42 is the latest WAL seq).
    """
    session = _CapturingSession(registry=_StubRewindRegistry())
    await _handler().handler(session, "")
    assert session.pending_command_ui == {
        "kind": "rewind",
        "points": [{"seq": 42, "kind": "turn"}, {"seq": 38, "kind": "turn"}],
    }
    assert [m.kind for m in session.outbox_msgs] == ["__rewind_list__"]
    assert "seq 42" in session.outbox_msgs[0].text


@pytest.mark.asyncio
async def test_bare_rewind_with_no_checkpoints_replies() -> None:
    """Tier 2: bare /rewind with no rewind points → a clear message, no picker."""
    class _Empty:
        def list_rewind_points(self, **_kw):
            return []
    session = _CapturingSession(registry=_Empty())
    await _handler().handler(session, "")
    assert session.pending_command_ui is None
    assert any("no earlier checkpoints" in m.text for m in session.outbox_msgs)


@pytest.mark.asyncio
async def test_rewind_with_seq_invokes_checkout(tmp_path) -> None:
    """Tier 2: /rewind <N> calls AgentRegistry.checkout(N) and reports success.

    The slash uses the SAME unified checkout the picker dispatches (D8) — no
    sibling-gap. Drives a real registry: WAL seq 1 + 2 appended, checkout to
    seq 1. The reply names the target seq; the WAL grows a reset-record.
    """
    reg = _make_registry(tmp_path)
    log = reg.state_log
    await log.append("inbox_put", target="alpha", msg_id="a", msg_kind="user", payload={})
    await log.append("inbox_put", target="alpha", msg_id="b", msg_kind="user", payload={})
    head_before = log.current_seq

    session = _CapturingSession(registry=reg)
    await _handler().handler(session, "1")

    # A reset-record was appended (checkout ran).
    assert log.current_seq > head_before
    # The reply names the target seq.
    texts = [getattr(m, "text", "") for m in session.outbox_msgs]
    assert any("seq 1" in t and "checked out" in t for t in texts)


@pytest.mark.asyncio
async def test_rewind_non_integer_arg_errors() -> None:
    """Tier 2: /rewind <non-int> surfaces a decision-enabling error, no crash."""
    session = _CapturingSession()
    await _handler().handler(session, "abc")
    assert [m.kind for m in session.outbox_msgs] == ["error"]
    assert "abc" in session.outbox_msgs[0].text


@pytest.mark.asyncio
async def test_rewind_seq_without_registry_errors() -> None:
    """Tier 2: /rewind <N> with no registry attached → error (not a crash)."""
    session = _CapturingSession(registry=None)
    await _handler().handler(session, "5")
    assert [m.kind for m in session.outbox_msgs] == ["error"]


@pytest.mark.asyncio
async def test_rewind_abandoned_target_checks_out_fork_switch(tmp_path) -> None:
    """Tier 2: /rewind <N> into an abandoned branch now SUCCEEDS (fork-switch).

    Contract reversal from the rewind_to era: rewind_to rejected an abandoned
    target (RewindIntoAbandonedError); the unified checkout (D8) has no
    active-target guard, so checking out a dead-branch seq revives that lineage
    — a fork-switch, not an error. Pins the new behaviour decisively.
    """
    from reyn.core.events.snapshot_generations import rewind as _rewind_record
    reg = _make_registry(tmp_path)
    log = reg.state_log
    await log.append("inbox_put", target="alpha", msg_id="a", msg_kind="user", payload={})
    await log.append("inbox_put", target="alpha", msg_id="b", msg_kind="user", payload={})
    await _rewind_record(log, target_n=1)  # abandons seq 2 (dead branch)
    head_before = log.current_seq

    session = _CapturingSession(registry=reg)
    await _handler().handler(session, "2")  # checkout the dead-branch seq

    # No error — the dead-branch checkout succeeded (fork-switch).
    assert "error" not in [m.kind for m in session.outbox_msgs]
    assert log.current_seq > head_before  # a reset-record reviving seq 2's lineage
    texts = [getattr(m, "text", "") for m in session.outbox_msgs]
    assert any("checked out" in t for t in texts)
