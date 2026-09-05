"""Tier 2: #4771 — ``AgentRegistry._await_quiescent_bounded``, the fail-safe
bound wrapping ``Session.await_quiescent()`` during a global rewind
(``checkout``/``rewind_to``).

``await_quiescent()`` itself stays unbounded by design (its own docstring's
"critical invariant" — see ``tests/core/test_await_quiescent.py`` and
``tests/core/test_4768_ephemeral_vanish_during_global_rewind.py``, which
deliberately do NOT wrap it, per lead-coder's own instruction there, so a
genuine termination bug in ``TrackedTaskSet.aclose`` hangs into CI's
``--timeout=120`` rather than being masked by a test-owned budget). THIS
file targets the NEW wrapper reyn's own rewind path adds one layer up
(``registry.py``'s ``_await_quiescent_bounded``) — real code this session
owns, not a third-party promise, so a controlled bound here is appropriate
(Tier 1/2's own discriminator).

Two things this file proves — ① by real execution (through the public
``checkout()`` surface), ② by asserting directly on
:func:`registry._quiesce_bound_s`'s own return value rather than racing a
real clock (lead-coder review, #4799: a sleep-vs-timeout margin test is
flaky under a loaded runner, and a failure there can't distinguish "the
formula broke" from "the runner was slow" — the fix is to test the pure
function the timeout is COMPUTED from, not the timing of the timeout
itself):

① A session that genuinely never quiesces makes the WHOLE ``checkout()``
   fail with :class:`RewindQuiesceTimeoutError` — never hangs forever, and
   never silently proceeds to append the reset-record (fail-safe,
   lead-coder-approved #4771 ruling ①).
② The bound VALUE scales with the held-MCP-connection count (#4771
   review's own catch: a single fixed constant would mis-fire the moment
   connection count grows, turning a healthy close into a false rewind
   failure — "the worst way to be wrong"). ``_quiesce_bound_s(0)`` floors
   to one unit; ``_quiesce_bound_s(3)`` is exactly three units.

``registry._MCP_CLIENT_CLOSE_WORST_CASE_S`` (the per-connection unit) is
monkeypatched down to a small value for ①'s real-execution test so it
runs fast without changing the logic under test — the PRODUCTION value
(6.5s, from reading the installed mcp SDK's own stdio teardown source) is
exercised for real in ``registry.py`` itself, unaffected by this patch
(module-global, restored by monkeypatch's own teardown).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import GLOBAL_SCOPE
from reyn.core.events.state_log import StateLog
from reyn.runtime import registry as registry_module
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import (
    AgentRegistry,
    RewindQuiesceTimeoutError,
    _quiesce_bound_s,
)
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry — mirrors test_4768's own helper."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _never_quiescent(*_a, **_kw):
    """A stand-in for ``Session.await_quiescent`` that genuinely never
    resolves — NOT a MagicMock (a plain coroutine function assigned to the
    real session instance), exercising the real ``asyncio.wait_for`` timeout
    path around a real awaitable that legitimately never completes."""
    return asyncio.Event().wait()  # an Event nobody ever .set()s


@pytest.mark.asyncio
async def test_a_session_that_never_quiesces_fails_the_rewind_not_hangs(
    tmp_path, monkeypatch,
):
    """Tier 2: ① fail-safe. A session whose await_quiescent() never resolves
    makes checkout() raise RewindQuiesceTimeoutError, bounded by a small
    per-connection unit (patched down for test speed) — never hangs, never
    proceeds to append the reset-record."""
    monkeypatch.setattr(registry_module, "_MCP_CLIENT_CLOSE_WORST_CASE_S", 0.1)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("alice")
    put_seq = await reg.state_log.append(
        "inbox_put", target="alice", msg_id="m1", msg_kind="user",
        payload={"text": "hi"},
    )
    monkeypatch.setattr(session, "await_quiescent", _never_quiescent)

    seq_before = reg.state_log.current_seq
    with pytest.raises(RewindQuiesceTimeoutError):
        await asyncio.wait_for(reg.checkout(put_seq, scope=GLOBAL_SCOPE), timeout=5.0)

    # No reset-record was ever appended — the failure happened BEFORE step 4.
    assert reg.state_log.current_seq == seq_before, (
        "a failed quiesce must abort before the reset-record append — "
        "any new WAL entry here would mean the fail-safe fired too late"
    )


def test_the_bound_value_scales_with_held_mcp_connection_count(monkeypatch):
    """Tier 1: ② the bound VALUE is not a single fixed constant — asserted
    directly on ``_quiesce_bound_s``'s own return value (no clock, no
    ``asyncio.wait_for`` race — lead-coder review, #4799). Pins the exact
    defect #4771's review caught: a fixed number would mis-fire a HEALTHY
    close the instant connection count grew; a correctly-scaled bound
    grows proportionally instead."""
    monkeypatch.setattr(registry_module, "_MCP_CLIENT_CLOSE_WORST_CASE_S", 0.15)
    assert _quiesce_bound_s(3) == pytest.approx(0.45)
    assert _quiesce_bound_s(1) == pytest.approx(0.15)


def test_zero_held_connections_still_gets_a_real_nonzero_bound(monkeypatch):
    """Tier 1: the floor (``max(1, held)``) — a connection-less session
    (the common case) is not raced against an effectively-zero timeout;
    ``_quiesce_bound_s(0)`` still returns one full unit, for the OTHER
    quiesce steps (``_turn_idle``, chain-timeout-watchdog cancellation)."""
    monkeypatch.setattr(registry_module, "_MCP_CLIENT_CLOSE_WORST_CASE_S", 0.15)
    assert _quiesce_bound_s(0) == pytest.approx(0.15)
