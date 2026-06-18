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

    async def _put_outbox(self, msg) -> None:
        self.outbox_msgs.append(msg)


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


@pytest.mark.asyncio
async def test_bare_rewind_emits_menu_sentinel() -> None:
    """Tier 2: bare /rewind emits the __rewind_menu__ sentinel (opens the picker)."""
    session = _CapturingSession()
    await _handler().handler(session, "")
    assert [m.kind for m in session.outbox_msgs] == ["__rewind_menu__"]


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
