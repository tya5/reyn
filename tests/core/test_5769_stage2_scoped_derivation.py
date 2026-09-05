"""Tier 2: OS invariant -- #5769 stage 2, design (B): derivation and scope
are separated so a caller states a plain fact ("this is my scope")
instead of building (and potentially mis-scoping) an opaque predicate.

Real `StateLog` + real on-disk stores (no mocks). Covers the two store
signature changes (`ConfigGenerationStore.latest_active`,
`PipelineStateStore.latest_active` now take `(state_log, scope)` directly)
and `latest_pipeline_state`'s own owner derivation (architect's #5772
finding: the owner was recorded, just not read at that call site --
fixed in the SAME PR per lead-coder-30's explicit ruling, not filed
separately). `list_rewind_points`'s per-agent scoping is covered
separately in `tests/core/test_registry_list_rewind_points_1f.py`'s own
sibling additions -- see that file for the (agent, sid) narrowing
witness; this file does not duplicate it.
"""
from __future__ import annotations

import pytest

from reyn.core.events.config_generations import ConfigGenerationStore
from reyn.core.events.pipeline_recovery import (
    PipelineStateStore,
    latest_pipeline_state,
    record_pipeline_state,
)
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import PipelineWorkOrder, pipeline_run_dir, write_invocation


async def _put(log: StateLog, agent: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id="x", msg_kind="user", payload={"text": "x"},
    )


@pytest.mark.asyncio
async def test_config_generation_store_latest_active_new_signature(tmp_path):
    """Tier 2: `ConfigGenerationStore.latest_active(rel_path, state_log,
    scope=...)` -- the new (state_log, scope) shape builds its own
    predicate and behaves identically to the old caller-hoisted-predicate
    shape for the (only real, ADR-0047-confirmed) GLOBAL_SCOPE case: a
    generation on an abandoned branch is excluded; one on the active
    branch is returned."""
    log = StateLog(tmp_path / "wal")
    store = ConfigGenerationStore(tmp_path / "generations")
    s1 = await _put(log, "alpha")
    store.record("reyn.yaml", {"v": 1}, s1)
    s2 = await _put(log, "alpha")
    store.record("reyn.yaml", {"v": 2}, s2)
    await log.append(REWIND_KIND, target_n=s1, supersedes=None)  # abandons s2

    latest = store.latest_active("reyn.yaml", log, scope=GLOBAL_SCOPE)

    assert latest == (s1, {"v": 1})


@pytest.mark.asyncio
async def test_config_generation_store_latest_active_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- a call site that forgets scope fails at the
    call, matching build_active_predicate's own required-kwarg contract
    this store's signature now delegates to."""
    log = StateLog(tmp_path / "wal")
    store = ConfigGenerationStore(tmp_path / "generations")

    with pytest.raises(TypeError):
        store.latest_active("reyn.yaml", log)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_pipeline_state_store_latest_active_new_signature(tmp_path):
    """Tier 2: `PipelineStateStore.latest_active(state_log, scope=...)` --
    same new shape, same equivalence check as the config store sibling."""
    log = StateLog(tmp_path / "wal")
    store = PipelineStateStore(tmp_path / "pipeline-gens")
    s1 = await _put(log, "alpha")
    store.record({"step_index": 1}, s1)
    s2 = await _put(log, "alpha")
    store.record({"step_index": 2}, s2)
    await log.append(REWIND_KIND, target_n=s1, supersedes=None)  # abandons s2

    latest = store.latest_active(log, scope=GLOBAL_SCOPE)

    assert latest == (s1, {"step_index": 1})


def _work_order(run_id: str, *, driver_agent: str, driver_sid: str, spawn_seq: int) -> PipelineWorkOrder:
    return PipelineWorkOrder(
        run_id=run_id, pipeline_name="p", pipeline={"steps": []}, input=None,
        reply_to_agent=driver_agent, reply_to_sid="main",
        driver_agent=driver_agent, driver_sid=driver_sid, spawn_seq=spawn_seq,
    )


@pytest.mark.asyncio
async def test_latest_pipeline_state_derives_owner_and_narrows_to_it(tmp_path):
    """Tier 2: strip-falsifier target for architect's #5772 finding.
    `latest_pipeline_state` now reads the SAME `invocation.json`
    `_rewake_pipeline_runs` already reads, deriving (driver_agent,
    driver_sid) -- a rewind SCOPED to a DIFFERENT (agent, sid) must not
    hide this run's own generation, and one scoped to THIS run's own
    driver must."""
    reyn_dir = tmp_path / ".reyn"
    log = StateLog(reyn_dir / "state" / "wal.jsonl")
    run_dir = pipeline_run_dir(reyn_dir, "run-1")
    s1 = await _put(log, "worker")
    write_invocation(run_dir, _work_order("run-1", driver_agent="worker", driver_sid="sidA", spawn_seq=s1))
    await record_pipeline_state(log, "run-1", {"step_index": 1}, durable=True)
    s2 = await _put(log, "worker")
    await record_pipeline_state(log, "run-1", {"step_index": 2}, durable=True)

    # A rewind scoped to a DIFFERENT (agent, sid) must not affect run-1.
    await log.append(
        REWIND_KIND, target_n=s1, supersedes=None, scope=["someone-else", "sidZ"],
    )
    still_visible = latest_pipeline_state("run-1", log)
    assert still_visible == {"step_index": 2}

    # A rewind scoped to run-1's OWN driver (worker, sidA) DOES abandon
    # the later generation -- proving the owner was genuinely derived and
    # applied, not silently defaulted to global (which would ALSO hide
    # this, making the first assertion the only real witness).
    await log.append(
        REWIND_KIND, target_n=s1, supersedes=None, scope=["worker", "sidA"],
    )
    now_hidden = latest_pipeline_state("run-1", log)
    assert now_hidden == {"step_index": 1}


@pytest.mark.asyncio
async def test_latest_pipeline_state_falls_back_to_global_without_invocation(tmp_path):
    """Tier 2: fail-closed to the SAFE side -- a run with generations but
    no readable invocation.json (should not happen in practice, not
    proven impossible) falls back to GLOBAL_SCOPE rather than raising or
    silently returning stale/wrong content."""
    reyn_dir = tmp_path / ".reyn"
    log = StateLog(reyn_dir / "state" / "wal.jsonl")
    s1 = await _put(log, "worker")
    await record_pipeline_state(log, "run-2", {"step_index": 1}, durable=True)
    # No write_invocation call -- invocation.json genuinely absent.

    result = latest_pipeline_state("run-2", log)

    assert result == {"step_index": 1}
