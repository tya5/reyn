"""Tier 1/2: #5851 stage (a) — the process-memory reader, `ProcessMemoryGuard`,
and the two REAL observation/record points (architect ruling ①: `load_history()`
= "起動", `_run_router_loop`'s `turn_end`-adjacent `finally` = "turn 終端").

No halt in this stage (architect ruling ③, lead-coder dispatch: halt/`SessionHalt`/
`remedies` land in a later PR). The other two observation points the ruling names
(`run_one_iteration`'s process-edge, the router-loop's in-turn iteration head) are
DECISION points for a halt check that does not exist yet — this file does not
wire or test them; see this PR's own body for the disclosed scope choice.

Real `Session`s throughout (`tests._support.agent_session.make_session`) — the
reader is the ONE injected seam (architect ruling ①: `Callable[[], int | None]`),
never a real allocation, `sleep`, or timer, per CLAUDE.md's testing policy on
duration-free tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.process_memory import (
    ProcessMemoryGuard,
    make_process_memory_reader,
    process_memory_metric_name,
)
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


def _session(tmp_path: Path, *, guard: "ProcessMemoryGuard | None" = None):
    return make_session(
        agent_name="pm-observe",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path / "ws",
        process_memory_guard=guard,
    )


# ── W6: the real reader (Tier 1 — a third-party/OS-level fact, not reyn's own) ──


def test_real_reader_returns_a_real_measurement_or_none_plus_unavailable():
    """Tier 1: on a platform this repo declares supported (darwin/linux),
    the REAL reader returns `int > 0` — a live syscall, no mock. On any
    other platform, `process_memory_metric_name()` is None and the
    default reader returns None too (never a fabricated number). The
    exact byte VALUE is third-party OS state — not pinned (CLAUDE.md:
    never pin algorithm-level/third-party behaviour); only the SHAPE
    (int>0, or None+metric-None) is this test's own claim."""
    metric = process_memory_metric_name()
    reader = make_process_memory_reader()
    value = reader()
    if metric is None:
        assert value is None
    else:
        assert metric in ("phys_footprint", "rss")
        assert isinstance(value, int)
        assert value > 0


def test_process_footprint_unavailable_fires_at_most_once_per_guard(tmp_path):
    """Tier 2: architect ruling ⑤ — "起動時1回" is PROCESS-scoped (the
    guard IS the process-wide instance), not per-call. Two sessions
    sharing ONE guard whose reader is forced to None see the
    `process_footprint_unavailable` audit-event exactly ONCE, on the
    first session's own load_history() — the second session's own
    load_history() produces no second announcement (same fact, not
    re-disclosed) but the reader is still consulted (no caching of the
    footprint READ itself, only of the announcement)."""
    guard = ProcessMemoryGuard(reader=lambda: None, metric=None, cap_bytes=None, enforce=False)
    s1 = _session(tmp_path / "s1", guard=guard)
    s2 = _session(tmp_path / "s2", guard=guard)
    events1 = collect_events(s1)
    events2 = collect_events(s2)

    s1.load_history()
    (only_unavailable,) = [
        e for e in events1 if e.type == "process_footprint_unavailable"
    ]
    assert only_unavailable.data.get("platform") == sys.platform

    s2.load_history()
    unavailable_2 = [e for e in events2 if e.type == "process_footprint_unavailable"]
    assert unavailable_2 == [], (
        "the SAME shared guard must not re-announce a platform fact that "
        "has not changed, on a second session in the same process"
    )


# ── W5: startup observation point (load_history) ────────────────────────────


def test_load_history_emits_exactly_one_process_footprint(tmp_path):
    """Tier 2: architect ruling ①, observation point ① ("起動") — accept
    W5. `load_history()` (empty-history early-return path here — the
    cheapest of its 2 internal paths, and this test's own point: the emit
    must fire regardless of which internal path ran) emits exactly one
    `process_footprint` carrying `bytes`/`metric`/`cap_bytes`/`enforce`.

    Falsify note (verified in-file, Edit-only, then reverted): removing
    the `finally: self._emit_process_footprint()` wrapper around
    `load_history`'s body makes this RED — zero events."""
    values = iter([4_000_000_000])
    guard = ProcessMemoryGuard(
        reader=lambda: next(values, None), metric="phys_footprint",
        cap_bytes=None, enforce=False,
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    s.load_history()

    (footprint,) = [e for e in events if e.type == "process_footprint"]
    assert footprint.data["bytes"] == 4_000_000_000
    assert footprint.data["metric"] == "phys_footprint"
    assert footprint.data["cap_bytes"] is None
    assert footprint.data["enforce"] is False
    assert footprint.data["chain_id"] is None


def test_load_history_emit_survives_a_missing_history_file(tmp_path):
    """Tier 2: the early-return path (`not self.history_path.exists()`)
    is the MOST likely real-world case (a brand-new session) — the emit
    must not be skipped just because there was nothing to hydrate."""
    values = iter([1_000])
    guard = ProcessMemoryGuard(
        reader=lambda: next(values, None), metric="phys_footprint",
        cap_bytes=None, enforce=False,
    )
    s = _session(tmp_path, guard=guard)
    assert not s.history_path.exists()
    events = collect_events(s)

    s.load_history()

    assert any(e.type == "process_footprint" for e in events)


# ── W2: enforce=false + over-cap reader -> no halt, but IS observed ─────────


def test_enforce_false_over_cap_does_not_halt_and_is_observed(tmp_path):
    """Tier 2: accept W2 — `enforce=False` with a reader forced ABOVE
    `cap_bytes` must NOT halt (stage (a) has no halt mechanism at all —
    this is also the trivial control that stage (c)'s own W2 will later
    tighten) AND must still emit `process_footprint{enforce: False,
    bytes > cap_bytes}` — "observe-only" must actually observe, not
    silently skip recording just because nothing acts on the reading."""
    values = iter([9_000_000_000])
    guard = ProcessMemoryGuard(
        reader=lambda: next(values, None), metric="phys_footprint",
        cap_bytes=1_000_000_000, enforce=False,
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    s.load_history()

    (footprint,) = [e for e in events if e.type == "process_footprint"]
    assert footprint.data["enforce"] is False
    assert footprint.data["bytes"] > footprint.data["cap_bytes"]
    assert s.halted_reason is None, (
        "stage (a) has no halt mechanism — an over-cap reading must never "
        "set halted_reason regardless of enforce"
    )


# ── observation point ④: turn-end (next to the turn_end hook dispatch) ─────


@pytest.mark.asyncio
async def test_turn_end_emits_process_footprint_with_chain_id(tmp_path):
    """Tier 2: architect ruling ①, observation point ④ ("turn 終端") —
    drives the REAL `_run_router_loop` (the method `turn_end`'s own
    dispatch lives in, `test_5248_turn_finally.py`'s own established
    pattern for reaching this finally chain without the LLM boundary) with
    `self._loop_driver.run_turn` stubbed to a no-op success. Asserts the
    SAME finally chain `turn_completed`/`turn_end` already reach also
    reaches the process-footprint emit, WITH this turn's own chain_id.

    Falsify note (verified in-file, Edit-only, then reverted): removing
    the `finally: self._emit_process_footprint(chain_id=chain_id)` call
    (leaving `_run_router_loop`'s hot_reloader step as the finally's only
    body) makes this RED — zero `process_footprint` events."""
    values = iter([2_000_000_000])
    guard = ProcessMemoryGuard(
        reader=lambda: next(values, None), metric="phys_footprint",
        cap_bytes=None, enforce=False,
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    async def _noop_run_turn(text: "str | None", chain_id: str) -> None:
        return None

    s._loop_driver.run_turn = _noop_run_turn  # type: ignore[method-assign]

    await s._run_router_loop("hello", "turn-end-chain")
    await settle(s)

    (footprint,) = [e for e in events if e.type == "process_footprint"]
    assert footprint.data["chain_id"] == "turn-end-chain"
    assert footprint.data["bytes"] == 2_000_000_000


# ── W4-adjacent: EVENT_AUDIT_REQUIREMENTS / AUDIT_EVENT_KINDS (Tier 1) ──────


def test_new_kinds_are_in_the_closed_vocabulary_and_have_requirements():
    """Tier 1: reyn's own property — the two new kinds are BOTH in the
    closed vocabulary (`AUDIT_EVENT_KINDS`) AND have field requirements
    declared (`EVENT_AUDIT_REQUIREMENTS`), matching every other emitted
    kind's shape. `test_audit_event_kind_vocabulary_3410.py` is the real
    census gate for "declared == emitted"; this test pins only the two
    registries' own internal consistency for these two kinds."""
    from reyn.core.events.event_schema import AUDIT_EVENT_KINDS, EVENT_AUDIT_REQUIREMENTS

    assert "process_footprint" in AUDIT_EVENT_KINDS
    assert "process_footprint_unavailable" in AUDIT_EVENT_KINDS
    assert EVENT_AUDIT_REQUIREMENTS["process_footprint"] >= {
        "bytes", "metric", "cap_bytes", "enforce",
    }
    assert EVENT_AUDIT_REQUIREMENTS["process_footprint_unavailable"] == {"platform"}
