"""Tier 2: OS invariant -- #5769 stage 1, reset-record scope plumbing.

Real `StateLog` (no mocks). Stage 1 adds ONLY the read-side scope field on
a rewind reset-record and makes `build_active_predicate`'s `scope` a
required keyword-only argument -- no writer for a non-`None` scope exists
yet (`checkout()`/`rewind()` are unchanged, byte-identical wire format).
Covers the 3 stage-1 acceptance points lead-coder-30's dispatch named:

  (1) the existing global rewind behaviour does not change one bit
      (the `scope=None` path)
  (2) `build_active_predicate` called without `scope` raises -- the
      startup-time witness that catches a missed one of the 12 real
      consumers
  (3) a legacy record (no `scope` key in the raw WAL entry -- what every
      record in existence today looks like) reads as `None` (global)

Also drives the READ-SIDE scope filter directly against a record carrying
an explicit non-`None` scope (injected via `StateLog.append`'s own
free-form kwargs, since no production writer emits one yet) -- proving
the plumbing stage 2 will build on top of actually behaves as designed,
not merely that it doesn't crash.
"""
from __future__ import annotations

import pytest

from reyn.core.events.snapshot_generations import (
    REWIND_KIND,
    build_active_predicate,
    is_active_seq,
    rewind,
)
from reyn.core.events.state_log import StateLog

AGENT = "alpha"


async def _put(log: StateLog, text: str) -> int:
    return await log.append(
        "inbox_put", target=AGENT, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


@pytest.mark.asyncio
async def test_scope_none_reproduces_identical_behavior_to_pre_5769(tmp_path):
    """Tier 2: acceptance point (1) -- strip-falsifier target. Global rewind (scope=None)
    must see EXACTLY the same active/abandoned seqs `is_active_seq` (the
    unchanged, scope-blind function) reports, for a real rewind chain."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")   # seq 1
    await _put(log, "b")   # seq 2
    await _put(log, "c")   # seq 3
    await rewind(log, target_n=1)  # seq 4 -- abandons (1, 4), i.e. 2 and 3

    is_active = build_active_predicate(log, scope=None)
    for seq in (1, 2, 3, 4):
        assert is_active(seq) == is_active_seq(log, seq), seq


def test_build_active_predicate_requires_scope_kwarg(tmp_path):
    """Tier 2: acceptance point (2) -- the startup-time witness for the 12 real consumers.
    calling `build_active_predicate` with no `scope` at all must raise,
    not silently default to global. This is what makes a missed call
    site fail loudly instead of quietly reasoning about the wrong
    (global, not its own session's) branch."""
    log = StateLog(tmp_path / "wal")

    with pytest.raises(TypeError):
        build_active_predicate(log)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_legacy_record_with_no_scope_field_reads_as_global(tmp_path):
    """Tier 2: acceptance point (3). A reset-record with no `scope` key at all (every record
    written by today's `checkout()`/`rewind()`, and every record that
    predates this field) is read back as scope=None -- global, visible to
    EVERY query scope, not just `scope=None`. The absence of the field is
    read as the SAFE side (global = broad reach), matching the issue's
    own explicit ruling."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")   # seq 1
    await _put(log, "b")   # seq 2
    # Written exactly the way rewind()/checkout() write it today -- no
    # `scope` kwarg at all.
    await log.append(REWIND_KIND, target_n=1, supersedes=None)  # seq 3

    global_view = build_active_predicate(log, scope=None)
    scoped_view = build_active_predicate(log, scope=("someone-else", "some-sid"))
    for seq in (1, 2, 3):
        assert global_view(seq) == scoped_view(seq), seq
    assert global_view(1) is True
    assert global_view(2) is False  # abandoned by the legacy-shaped record


@pytest.mark.asyncio
async def test_scoped_record_is_invisible_outside_its_own_scope(tmp_path):
    """Tier 2: the READ-SIDE filter itself, driven directly (no writer
    exists in production yet, so this injects a scoped record the same
    way a future stage-2 writer would -- `StateLog.append`'s own
    free-form kwargs). A record scoped to (agentA, sid1) must NOT abandon
    anything for a DIFFERENT scope's query, nor for the global query --
    only its own scope's query sees it."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")   # seq 1
    await _put(log, "b")   # seq 2
    await log.append(
        REWIND_KIND, target_n=1, supersedes=None, scope=["agentA", "sid1"],
    )  # seq 3, scoped

    own_scope = build_active_predicate(log, scope=("agentA", "sid1"))
    other_scope = build_active_predicate(log, scope=("agentB", "sid2"))
    global_scope = build_active_predicate(log, scope=None)

    assert own_scope(2) is False    # abandoned, from its own scope's view
    assert other_scope(2) is True   # untouched -- a different session's rewind
    assert global_scope(2) is True  # untouched -- global is not narrowed by a scoped record


@pytest.mark.asyncio
async def test_scoped_and_global_records_compose_for_the_owning_scope(tmp_path):
    """Tier 2: a scope's own chain = global records UNION its own-scope
    records (issue's own stated composition) -- both a prior GLOBAL
    rewind and a later SCOPED one abandon seqs for a query matching that
    scope, while a query for an unrelated scope only sees the global
    one."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")   # seq 1
    await _put(log, "b")   # seq 2
    await _put(log, "c")   # seq 3
    await rewind(log, target_n=1)  # seq 4, global -- abandons (1, 4)
    await _put(log, "d")   # seq 5 (post-rewind work, active)
    await _put(log, "e")   # seq 6
    await log.append(
        REWIND_KIND, target_n=5, supersedes=None, scope=["agentA", "sid1"],
    )  # seq 7, scoped -- abandons (5, 7) for agentA/sid1 only

    own_scope = build_active_predicate(log, scope=("agentA", "sid1"))
    other_scope = build_active_predicate(log, scope=("agentB", "sid2"))

    # Both queries see the global abandonment (2, 3).
    assert own_scope(2) is False and other_scope(2) is False
    # Only the owning scope sees its own additional abandonment (6).
    assert own_scope(6) is False
    assert other_scope(6) is True
