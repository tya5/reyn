"""Tier 2: #5939 / #5851 stage (b) PR-2 — the steady-state memory ladder
(① backpressure → ② cache release [PR-1's shared step] → ③ session halt →
④ process exit) and the structural remedies fix (``Session._latch_halt``,
the ONE chokepoint every halt reason — durability, shutdown, cancellation,
process-memory — now goes through).

Real `Session`s throughout (`tests._support.agent_session.make_session`),
a real injected `ProcessMemoryGuard.reader` (never a Mock — the SAME
injection seam `test_5851a_process_memory_observe.py` already
establishes), a real `AgentRegistry` with real co-resident sessions for
the ④ broadcast tests. `os._exit` is intercepted (raising a sentinel
exception instead) since the real call would kill the test worker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import SessionHaltError
from tests._support.agent_session import make_session
from tests._support.events import collect_events


def _fake_reader_sequence(values: "list[int | None]"):
    """A real, injectable callable — never a Mock — returning each of
    *values* in order, then repeating the last one."""
    it = iter(values)
    last = values[-1] if values else None

    def _read() -> "int | None":
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return _read


def _session(tmp_path: Path, *, guard: "ProcessMemoryGuard | None" = None, registry=None):
    return make_session(
        agent_name="pr2-agent",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path / "ws",
        process_memory_guard=guard,
        registry=registry,
    )


class _ExitCalled(Exception):
    """Sentinel raised by the intercepted ``os._exit`` — never a real
    process termination inside a test."""


def _intercept_exit(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    calls: "list[int]" = []

    def _fake_exit(code: int) -> None:
        calls.append(code)
        raise _ExitCalled()

    import os
    monkeypatch.setattr(os, "_exit", _fake_exit)
    return calls


# ── _latch_halt: structural remedies enforcement ────────────────────────


def test_latch_halt_has_no_default_for_remedies(tmp_path: Path):
    """Tier 2: the structural half of the fix — ``remedies`` is a
    REQUIRED parameter with NO default, so a 5th call site that forgets
    it gets ``TypeError`` at the call, never a silently-empty
    ``session_halted``."""
    s = _session(tmp_path)
    with pytest.raises(TypeError):
        s._latch_halt("some_reason")  # type: ignore[call-arg]


def test_latch_halt_rejects_an_explicit_empty_remedies_tuple(tmp_path: Path):
    """Tier 2: the runtime half — defense in depth for the one shape the
    signature alone cannot catch (an explicit empty tuple).

    Strip: comment out the ``if not remedies: raise ValueError(...)``
    guard in ``_latch_halt`` — this goes RED (no exception, a halt with
    no remedies is constructed, performed during review)."""
    s = _session(tmp_path)
    with pytest.raises(ValueError, match="empty remedies"):
        s._latch_halt("some_reason", remedies=())


def test_latch_halt_first_reason_wins(tmp_path: Path):
    """Tier 2: matches every pre-existing call site's own
    ``_halted_reason is None`` guard — a session already halted for
    reason A is not overwritten by reason B."""
    s = _session(tmp_path)
    s._latch_halt("reason-a", remedies=("do a",))
    s._latch_halt("reason-b", remedies=("do b",))
    assert s.halted_reason == "reason-a"
    assert s.halt_remedies == ("do a",)


# ── accept-edge: ANY halt reason blocks further ops, not just durability ──


def test_accept_edge_stays_durability_specific_for_a_non_durability_reason(
    tmp_path: Path,
):
    """Tier 2: corrected design (lead-coder review, caught by #5214's own
    MessageBus test suite going red): the ACCEPT-edge
    (``_fail_stop_if_durability_dead``) does NOT raise for a
    non-durability reason -- an earlier version of this PR generalized
    it to raise for ANY latched reason, which broke #5214's own
    established contract (a message submitted after ``shutdown_
    requested``/``cancelled`` must still be safely QUEUED, durability
    being perfectly healthy for those reasons -- it simply never gets
    PROCESSED, because the PROCESS-edge, not the accept-edge, is what
    actually stops the loop). See ``_fail_stop_if_durability_dead``'s
    own docstring for the full reasoning."""
    s = _session(tmp_path)
    s._latch_halt("process_memory", remedies=("restart",))

    s._fail_stop_if_durability_dead()  # must NOT raise


def test_process_edge_returns_false_for_any_already_latched_halt_reason(
    tmp_path: Path,
):
    """Tier 2: this is where a non-durability halt reason (e.g.
    ``process_memory``) actually gets its teeth -- ``run_one_iteration``
    (the PROCESS-edge, polled by ``run()``'s own ``while await self.
    run_one_iteration():`` loop) returns False for ANY already-latched
    reason, checked first, causing the loop to exit and ``run_completed``
    to become True. Without this, a halt announced via `_latch_halt`
    alone would never actually stop further inbox items from being
    pumped.

    Strip: revert the process-edge's own leading ``if self._halted_
    reason is not None: return False`` -- this goes RED (a real inbox
    item gets processed despite the latched halt, performed during
    review)."""
    import asyncio

    s = _session(tmp_path)
    s._latch_halt("process_memory", remedies=("restart",))

    result = asyncio.run(s.run_one_iteration())

    assert result is False


@pytest.mark.asyncio
async def test_durability_halt_still_raises_and_carries_nonempty_remedies(tmp_path: Path):
    """Tier 2: FP gate — the pre-existing durability path is unchanged in
    its own raise behavior, and now ALSO carries non-empty remedies
    (previously none existed for this reason at all). Same real
    §4-exhausted fire-and-forget failure injection
    `test_2259_pr3_recovery_semantics_falsify.py`'s own
    `test_durability_failure_fail_stops_and_surfaces` already
    establishes -- `durability_failed` is a read-only computed property,
    not a field a test may set directly."""
    from reyn.core.events.durability_worker import DurabilityWorker

    worker = DurabilityWorker(max_write_attempts=1)
    log = StateLog(tmp_path / "wal.jsonl", worker=worker)
    s = make_session(agent_name="pr2-agent", state_log=log)

    async def _boom() -> None:
        raise OSError("simulated disk death")

    log.submit_durable_nowait(_boom)
    await log.flush()
    assert log.durability_failed  # sanity

    with pytest.raises(SessionHaltError):
        s._fail_stop_if_durability_dead()

    assert s.halted_reason == "durability_failure"
    assert s.halt_remedies
    assert len(s.halt_remedies) >= 1


# ── the steady-state ladder: ① → ② → ③ → ④, driven directly ─────────────


def test_ladder_is_inert_when_not_enforced(tmp_path: Path):
    """Tier 2: matches every other stage (a)/(b) mechanism's own default
    -- `enforce=False`/`cap_bytes=None` means 0 lines run, even with a
    reader forced far over any plausible cap."""
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([999_999_999_999]), cap_bytes=1, enforce=False,
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    s._check_memory_ladder(chain_id=None, footprint=999_999_999_999)

    assert events == []
    assert s.halted_reason is None


def test_ladder_enters_backpressure_over_cap_and_clears_when_it_drops(tmp_path: Path):
    """Tier 2: ① accept -- crossing the cap latches
    ``session_memory_backpressure`` exactly once (not per-call while
    still over), and the NEXT sample under cap clears it."""
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([0]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    s._check_memory_ladder(chain_id="c1", footprint=150)  # over cap -> enter
    s._check_memory_ladder(chain_id="c1", footprint=150)  # still over, no compaction seen -> no-op
    s._check_memory_ladder(chain_id="c1", footprint=50)  # back under -> cleared

    entered = [e for e in events if e.type == "session_memory_backpressure"]
    cleared = [e for e in events if e.type == "session_memory_backpressure_cleared"]
    (only_entered,) = entered
    (only_cleared,) = cleared
    assert only_entered.data["bytes"] == 150
    assert only_entered.data["cap_bytes"] == 100
    assert s.halted_reason is None


def test_ladder_escalates_to_halt_and_exit_when_still_over_after_compaction_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the FULL escalation path -- ① backpressure, a
    `compaction_completed` observed while backpressure is active arms
    the judge, ② (PR-1's real cache-release) runs and does not bring the
    (fake, fixed) reader back under cap, ③ latches `session_halted
    {reason="process_memory"}`, and ④ calls the (intercepted) process
    exit. `os._exit` is never allowed to actually run in this test.

    Strip: comment out the `if not self._compaction_seen_since_
    backpressure: return` guard -- this goes RED (escalates on the very
    FIRST over-cap sample, no compaction needed, performed during
    review)."""
    exit_calls = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    s._check_memory_ladder(chain_id="c1", footprint=500)  # ① enter backpressure
    assert s.halted_reason is None  # sanity: not escalated yet

    s._audit_events.emit("compaction_completed")  # arms the judge
    with pytest.raises(_ExitCalled):
        s._check_memory_ladder(chain_id="c1", footprint=500)  # ②③④

    assert s.halted_reason == "process_memory"
    assert s.halt_remedies
    assert exit_calls == [1]

    kinds = [e.type for e in events]
    assert "session_memory_backpressure" in kinds
    assert "process_memory_forensics" in kinds  # PR-1's own step ② ran for real
    assert "session_halted" in kinds
    assert "session_memory_exit" in kinds
    (halted,) = [e for e in events if e.type == "session_halted"]
    assert halted.data["reason"] == "process_memory"
    assert halted.data["remedies"]


def test_ladder_does_not_escalate_before_a_compaction_is_observed(tmp_path: Path):
    """Tier 2: deny side of the escalation judge -- repeated over-cap
    samples with NO `compaction_completed` in between never reach ③,
    however many times the ladder is checked."""
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = _session(tmp_path, guard=guard)

    for _ in range(5):
        s._check_memory_ladder(chain_id="c1", footprint=500)

    assert s.halted_reason is None


# ── ④ broadcast: registry-absent vs registry-present ────────────────────


def test_memory_exit_broadcast_path_none_without_a_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: no ``registry`` attached (embedded/test session) --
    ``broadcast_path="none"``, ``session_count=1`` — the degenerate,
    no-broadcast-channel case, distinguished from a registry genuinely
    holding one session (see the sibling test below)."""
    exit_calls = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(reader=_fake_reader_sequence([1]), cap_bytes=1, enforce=True)
    s = _session(tmp_path, guard=guard)  # constructed with no `registry=` kwarg
    events = collect_events(s)

    with pytest.raises(_ExitCalled):
        s._memory_exit(footprint=200, remedies=("restart",))

    assert exit_calls == [1]
    (exit_event,) = [e for e in events if e.type == "session_memory_exit"]
    assert exit_event.data["broadcast_path"] == "none"
    assert exit_event.data["session_count"] == 1
    assert exit_event.data["delivered"] == 1
    assert s.halted_reason == "process_memory"


def test_memory_exit_broadcasts_to_every_co_resident_session_via_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the real fan-out -- a real ``AgentRegistry`` with 2 real,
    independently-constructed sessions. Triggering ④ on ONE of them must
    latch ``process_memory`` on BOTH (the co-resident one never touched
    its own cap), and ``session_count``/``delivered`` must both be 2 --
    not the degenerate 1-with-no-channel shape the sibling test covers."""
    exit_calls = _intercept_exit(monkeypatch)

    def _factory(profile):
        return sessions_by_name[profile.name]

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=None)
    sessions_by_name = {}
    for name in ("pr2-agent-a", "pr2-agent-b"):
        AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)
        sessions_by_name[name] = make_session(
            agent_name=name,
            state_log=StateLog(tmp_path / ".reyn" / f"wal-{name}.jsonl"),
            snapshot_path=tmp_path / f"snap-{name}.json",
            workspace_state_dir=tmp_path / f"ws-{name}",
            registry=registry,
        )
        registry.get_or_load(name)

    triggering = registry.get_or_load("pr2-agent-a")
    other = registry.get_or_load("pr2-agent-b")
    assert other.halted_reason is None  # sanity: not yet touched
    events = collect_events(triggering)

    with pytest.raises(_ExitCalled):
        triggering._memory_exit(footprint=200, remedies=("restart",))

    assert exit_calls == [1]
    assert triggering.halted_reason == "process_memory"
    assert other.halted_reason == "process_memory", (
        "the co-resident session (never over its own cap) must still receive "
        "the broadcast halt -- process_memory is a PROCESS-wide resource"
    )
    (exit_event,) = [e for e in events if e.type == "session_memory_exit"]
    assert exit_event.data["broadcast_path"] == "registry"
    assert exit_event.data["session_count"] == 2
    assert exit_event.data["delivered"] == 2


def test_agent_registry_all_sessions_returns_every_live_session(tmp_path: Path):
    """Tier 2: the new public wrapper -- driven against a value this
    test already has independently (the sessions it just constructed),
    never against the private ``_iter_sessions`` this wraps (that would
    be validating the wrapper against itself)."""

    def _factory(profile):
        return sessions_by_name[profile.name]

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=None)
    sessions_by_name = {}
    for name in ("pr2-a", "pr2-b"):
        AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)
        sessions_by_name[name] = make_session(
            agent_name=name,
            state_log=StateLog(tmp_path / ".reyn" / f"wal-{name}.jsonl"),
            snapshot_path=tmp_path / f"snap-{name}.json",
            workspace_state_dir=tmp_path / f"ws-{name}",
        )
        registry.get_or_load(name)

    assert set(registry.all_sessions()) == set(sessions_by_name.values())
