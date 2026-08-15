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

Two things this file proves, both by real execution:

① A session that genuinely never quiesces makes the WHOLE ``checkout()``
   fail with :class:`RewindQuiesceTimeoutError` — never hangs forever, and
   never silently proceeds to append the reset-record (fail-safe, #4771
   owner ruling ①).
② The bound SCALES with the session's own held-MCP-connection count
   (#4771 review's own catch: a single fixed constant would mis-fire the
   moment connection count grows, turning a healthy close into a false
   rewind failure — "the worst way to be wrong"). A session reporting a
   HIGHER held-connection count gets a proportionally longer bound before
   ``_await_quiescent_bounded`` gives up.

``registry._MCP_CLIENT_CLOSE_WORST_CASE_S`` (the per-connection unit) is
monkeypatched down to a small value so these tests run fast without
changing the logic under test — the PRODUCTION value (6.5s, from reading
the installed mcp SDK's own stdio teardown source) is exercised for real
in ``registry.py`` itself, unaffected by this patch (module-global,
restored by monkeypatch's own teardown).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime import registry as registry_module
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry, RewindQuiesceTimeoutError
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


def _quiescent_after(delay: float):
    def _inner(*_a, **_kw):
        return asyncio.sleep(delay)
    return _inner


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
        await asyncio.wait_for(reg.checkout(put_seq), timeout=5.0)

    # No reset-record was ever appended — the failure happened BEFORE step 4.
    assert reg.state_log.current_seq == seq_before, (
        "a failed quiesce must abort before the reset-record append — "
        "any new WAL entry here would mean the fail-safe fired too late"
    )


@pytest.mark.asyncio
async def test_the_bound_scales_with_held_mcp_connection_count(tmp_path, monkeypatch):
    """Tier 2: ② the bound is not a single fixed constant — a session
    reporting MORE held MCP connections gets a proportionally longer
    window before _await_quiescent_bounded gives up. Pins the exact defect
    #4771's review caught: a fixed number would mis-fire a HEALTHY close
    the instant connection count grew."""
    monkeypatch.setattr(registry_module, "_MCP_CLIENT_CLOSE_WORST_CASE_S", 0.15)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("alice")

    # A quiesce that takes longer than ONE connection's worst case (0.15s)
    # but less than THREE connections' worth (0.45s) — a fixed single-unit
    # bound would wrongly fail this; a correctly-scaled bound must not.
    monkeypatch.setattr(session, "await_quiescent", _quiescent_after(0.30))
    monkeypatch.setattr(session, "mcp_held_servers", lambda: ["a", "b", "c"])

    # Must NOT raise — 3 held connections -> bound = 3 * 0.15 = 0.45s > 0.30s.
    await reg._await_quiescent_bounded(session)


@pytest.mark.asyncio
async def test_zero_held_connections_still_gets_a_real_nonzero_bound(
    tmp_path, monkeypatch,
):
    """Tier 2: the floor (max(1, held)) — a connection-less session (the
    common case) is not raced against an effectively-zero timeout; it gets
    at least one full unit for the OTHER quiesce steps (_turn_idle,
    chain-timeout-watchdog cancellation)."""
    monkeypatch.setattr(registry_module, "_MCP_CLIENT_CLOSE_WORST_CASE_S", 0.15)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("alice")

    monkeypatch.setattr(session, "await_quiescent", _quiescent_after(0.05))
    monkeypatch.setattr(session, "mcp_held_servers", lambda: [])

    # Must NOT raise -- 0 held connections still floors to 1 unit (0.15s),
    # comfortably above the 0.05s this quiesce actually takes.
    await reg._await_quiescent_bounded(session)
