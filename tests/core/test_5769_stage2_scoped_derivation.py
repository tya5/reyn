"""Tier 2: OS invariant -- #5769 stage 2, design (B): derivation and scope
are separated so a caller states a plain fact ("this is my scope")
instead of building (and potentially mis-scoping) an opaque predicate.

Real `StateLog` + real on-disk stores (no mocks). Covers the two store
signature changes (`ConfigGenerationStore.latest_active`,
`PipelineStateStore.latest_active` now take `(state_log, scope)` directly).

`latest_pipeline_state`'s own scoping test moved to
`test_5769_stage3_pipeline_resume_scope.py` (#5769 stage 3, architect's
(c) ruling on PR #5778 superseded the #5772/stage-2 invocation.json-
reading design this file originally tested here -- that mechanism no
longer exists; `latest_pipeline_state` now takes `scope` directly from
its caller, who already holds it). `list_rewind_points`'s per-agent
scoping is covered separately in
`tests/core/test_registry_list_rewind_points_1f.py`'s own sibling
additions -- see that file for the (agent, sid) narrowing witness; this
file does not duplicate it.
"""
from __future__ import annotations

import pytest

from reyn.core.events.config_generations import ConfigGenerationStore
from reyn.core.events.pipeline_recovery import PipelineStateStore
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND
from reyn.core.events.state_log import StateLog


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


