"""Tier 2: #4405 — the ``REYN_STALL_TRACE`` wiring in
``Session._run_router_loop`` actually calls ``reyn.runtime.stall_trace``'s
real ``arm``/``disarm``.

``stall_trace.py``'s own module docstring (per lead-coder's review) claimed
this wiring was tested when it was not — a declared-vs-actual gap in the
exact class this session's own convention exists to catch. This closes it
with the one test lead-coder's review asked for: real ``arm``/``disarm``
calls observed through a public seam (monkeypatched to plain recorder
functions — not ``unittest.mock`` — so the assertion is "was the real
function replaced and invoked", never a private-state read).

No waiting, no sleeping, no threshold crossing: the point under test is
WIRING (does the env var reaching a turn cause arm-then-disarm to be
called with the right value), not the N-second stall-detection behavior
itself, which stall_trace.py's own docstring already explains cannot be
tested without violating the testing-policy time-limit ban — see that
module for why no test exercises the actual firing.

``monkeypatch.setattr(stall_trace, "arm", ...)`` only intercepts the call
because ``Session._run_router_loop`` does ``from reyn.runtime.stall_trace
import arm as _arm_stall_trace`` INSIDE the method body (a fresh
module-attribute lookup on every call), not at module import time. If
that import were ever hoisted to session.py's top level, it would bind
``_arm_stall_trace`` to the original function object once at import time
— this file's ``monkeypatch.setattr`` would then silently stop
intercepting anything, and all three tests here would go green for the
wrong reason (nothing left to fail). Keep the import function-local if
you touch that call site.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime import stall_trace
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


@pytest.mark.asyncio
async def test_stall_trace_armed_and_disarmed_around_a_turn_when_env_set(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: with REYN_STALL_TRACE set, a turn calls stall_trace.arm(N)
    before RouterLoopDriver.run_turn() and stall_trace.disarm() after —
    real functions swapped for recorders, real call observed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _make_session(tmp_path)

    calls: list[str] = []

    def _fake_arm(seconds: float) -> None:
        calls.append(f"arm:{seconds}")

    def _fake_disarm() -> None:
        calls.append("disarm")

    monkeypatch.setattr(stall_trace, "arm", _fake_arm)
    monkeypatch.setattr(stall_trace, "disarm", _fake_disarm)

    async def _noop_run_turn(user_text: str, chain_id: str) -> None:
        # arm() must have already run by the time run_turn is reached.
        assert calls == ["arm:5.0"], (
            "arm() must fire BEFORE RouterLoopDriver.run_turn(), not after"
        )

    session._loop_driver.run_turn = _noop_run_turn  # type: ignore[method-assign]

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    result = await session.run_one_iteration()

    assert result is True
    assert calls == ["arm:5.0", "disarm"], (
        "expected exactly one arm() then one disarm() bracketing the turn"
    )


@pytest.mark.asyncio
async def test_stall_trace_not_touched_when_env_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: accept-side — with REYN_STALL_TRACE unset (the default),
    neither arm() nor disarm() is called. Proves the wiring costs nothing
    for the overwhelming majority of turns that never opt in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)
    session = _make_session(tmp_path)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _noop_run_turn(user_text: str, chain_id: str) -> None:
        pass

    session._loop_driver.run_turn = _noop_run_turn  # type: ignore[method-assign]

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    result = await session.run_one_iteration()

    assert result is True
    assert calls == [], "arm/disarm must not be touched when the env var is unset"


@pytest.mark.asyncio
async def test_stall_trace_disarmed_even_when_the_turn_raises(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: disarm() fires on the EXCEPTION path too, not just the happy
    path — the ``finally`` block's own reason for existing. Without this,
    a turn that raises leaves the background timer armed past turn end:
    it later dumps an UNRELATED stack into reyn.log, a false lead in
    exactly the shape #4403's investigation was fighting. Removing the
    ``finally`` (or the disarm call inside it) would leave the other two
    tests in this file green — only an exception-path turn exercises
    this line, so it needed its own witness."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _make_session(tmp_path)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_turn(user_text: str, chain_id: str) -> None:
        raise RuntimeError("simulated turn failure")

    session._loop_driver.run_turn = _raising_run_turn  # type: ignore[method-assign]

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    # The router loop's own top-level handler logs-and-swallows an
    # exception no inner handler took (session.py's "router loop caught
    # an exception no inner handler took" path, #5332) rather than
    # propagating it out of run_one_iteration — so this await completes
    # normally; the turn's FAILURE is not what this test is about, only
    # whether disarm() ran.
    await session.run_one_iteration()

    assert calls == ["arm", "disarm"], (
        "disarm() must still fire after a turn that raised — an armed "
        "timer left running past turn end dumps an unrelated stack later"
    )
