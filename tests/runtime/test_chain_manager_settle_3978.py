"""Tier 2: OS invariant — ChainManager.settle() (proposal 0067, "the settle path").

ADR-0040 D4: "push-at-settle with immediate deletion" — a task's on_settle
disposition (``"deliver"`` | ``"<pipeline name>"`` | ``"drop"``) executes,
then its handle is popped, IN THE SAME FUNCTION (mirroring ``resolve()``'s
existing pop+cancel_timeout+journal shape — this is the acceptance
condition: ONE settle function, no ``pipeline``/``run_id`` in its
signature). ``delegate_to_agent``'s own chain-resolve completion never
folds into this function — architect ruling, #3978: P6 retired the tool
with no replacement producer, so its (permanently non-task) chains stay on
``resolve()``, never ``settle()``.

Real ``ChainManager``/``SnapshotJournal``/``StateLog`` throughout — no
mocks, matching ``test_chain_manager_find_chain.py``'s established pattern.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.task_types import Requester


def _make_manager(tmp_path: Path) -> ChainManager:
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha", snapshot_path=tmp_path / "snap.json", state_log=log,
    )
    return ChainManager(
        journal=journal, events=EventLog(), chain_timeout_seconds=0, max_hop_depth=10,
    )


# ── P4 field decision: kind (added) + requester (derived, not stored) ──────


@pytest.mark.asyncio
async def test_registered_kind_is_readable_back(tmp_path: Path):
    """Tier 2: #3978 P4 — a handle registered with ``kind`` is readable back
    via ``get``/``find_chain`` (describe_task/list_tasks's own read path)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-k1", depth=0, original_text="p", sender=None,
        kind="pipeline",
    )
    assert mgr.get("run-k1").kind == "pipeline"


@pytest.mark.asyncio
async def test_registered_kind_defaults_to_none_for_legacy_delegate_chains(
    tmp_path: Path,
):
    """Tier 2: falsification pair — a caller that doesn't pass ``kind`` (every
    existing delegate-relay call site, unchanged by this PR) still registers
    cleanly, with ``kind=None`` — "not yet a typed task" is a real, distinct
    value from any of prompt/pipeline/exec, not a crash."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-k2", depth=0, original_text="p", sender=None,
    )
    assert mgr.get("run-k2").kind is None


@pytest.mark.asyncio
async def test_requester_derives_from_origin_agent_and_sid(tmp_path: Path):
    """Tier 2: ADR-0040 D6 — ``requester`` is NOT a stored field (lead-coder's
    measurement, #3978): it's a read accessor over the EXISTING
    (origin_agent, origin_sid) fields, which already carry the same fact
    (the docstring's own "routes back to the specific (origin_agent,
    origin_sid)"). Real, non-default sid."""
    mgr = _make_manager(tmp_path)
    chain = await mgr.register(
        chain_id="run-k3", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s7"),
    )
    assert chain.requester.agent_name == "worker"
    assert chain.requester.session_id == "s7"


@pytest.mark.asyncio
async def test_requester_session_id_defaults_to_main_when_origin_sid_none(
    tmp_path: Path,
):
    """Tier 2: falsification pair — ``origin_sid=None`` (the main-session
    case, #2130's own documented default) derives ``requester.session_id ==
    "main"``, matching every other main-case convention in this codebase
    (e.g. ``PipelineWorkOrder.reply_to_sid``), not a bare ``None`` that
    ``Requester``'s own type (``session_id: str``) can't even represent."""
    mgr = _make_manager(tmp_path)
    chain = await mgr.register(
        chain_id="run-k4", depth=0, original_text="p", sender="worker",
    )
    assert chain.requester.session_id == "main"


# ── acceptance condition ①: one settle function, kind-agnostic signature ───


def test_settle_signature_names_no_pipeline_or_run_id():
    """Tier 1: the settle-path acceptance condition itself, grep-checkable
    at the type level — ``ChainManager.settle`` takes no parameter literally
    named ``pipeline`` or ``run_id`` (the generic ``chain_id`` is reused,
    same as every other ChainManager method) — a kind-agnostic signature
    generic enough for a future task-kind producer to reuse without a
    signature change. (``launch_pipeline`` — the disposition-execution
    callback for the "<pipeline name>" case, D4 — is exempt by design: the
    condition bars a pipeline-RUN-SPECIFIC argument like a raw ``pipeline``
    definition object or a ``run_id`` string, not the word "pipeline"
    appearing anywhere, since "<pipeline name>" is itself a generic
    disposition any task kind can choose.)"""
    params = set(inspect.signature(ChainManager.settle).parameters)
    assert "pipeline" not in params
    assert "run_id" not in params


# ── on_settle dispatch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settle_deliver_runs_the_deliver_callback(tmp_path: Path):
    """Tier 2: on_settle="deliver" awaits the caller's own deliver callback."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-1", depth=0, original_text="p", sender=None,
    )
    calls = []

    async def _deliver() -> None:
        calls.append("delivered")

    await mgr.settle("run-1", on_settle="deliver", deliver=_deliver)
    assert calls == ["delivered"]


@pytest.mark.asyncio
async def test_settle_drop_never_calls_deliver(tmp_path: Path):
    """Tier 2: on_settle="drop" is a no-op disposition — deliver is never
    called (ADR-0040 D4: the disposition is intentionally discarded)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-2", depth=0, original_text="p", sender=None,
    )
    calls = []

    async def _deliver() -> None:
        calls.append("delivered")

    await mgr.settle("run-2", on_settle="drop", deliver=_deliver)
    assert calls == []


@pytest.mark.asyncio
async def test_settle_pipeline_name_without_launcher_raises_not_implemented(
    tmp_path: Path,
):
    """Tier 2: an on_settle value that isn't "deliver"/"drop" is a pipeline
    NAME (ADR-0040 D4) — without a ``launch_pipeline`` callback this fails
    LOUD (NotImplementedError), not as a silent no-op, since the actual
    launch-and-reroute mechanism is still unbuilt (proposal 0067 P4/P7)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-3", depth=0, original_text="p", sender=None,
    )

    async def _deliver() -> None:
        pass

    with pytest.raises(NotImplementedError):
        await mgr.settle("run-3", on_settle="filter_pipeline", deliver=_deliver)


@pytest.mark.asyncio
async def test_settle_pipeline_name_with_launcher_calls_it_with_the_name(
    tmp_path: Path,
):
    """Tier 2: accept-side sibling — a caller that DOES pass
    ``launch_pipeline`` gets it invoked with the on_settle value (the
    pipeline name), not ``deliver``."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-4", depth=0, original_text="p", sender=None,
    )
    launched = []

    async def _deliver() -> None:
        raise AssertionError("deliver must not run for a pipeline-name disposition")

    async def _launch(name: str) -> None:
        launched.append(name)

    await mgr.settle(
        "run-4", on_settle="filter_pipeline", deliver=_deliver, launch_pipeline=_launch,
    )
    assert launched == ["filter_pipeline"]


# ── immediate deletion, same function as the disposition ───────────────────


@pytest.mark.asyncio
async def test_settle_pops_the_handle_in_the_same_call(tmp_path: Path):
    """Tier 2: ADR-0040 D4 "immediate deletion" — after settle() returns,
    the handle is gone from the manager (mirrors resolve()'s pop)."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-5", depth=0, original_text="p", sender=None,
    )
    assert mgr.has("run-5")

    async def _deliver() -> None:
        pass

    await mgr.settle("run-5", on_settle="deliver", deliver=_deliver)

    assert not mgr.has("run-5")
    assert mgr.find_chain("run-5") is None


@pytest.mark.asyncio
async def test_settle_returns_the_popped_handle(tmp_path: Path):
    """Tier 2: settle() returns the popped _PendingChain (same as resolve()'s
    own return contract) — a caller can still read origin_agent/origin_sid
    off the return value after it's gone from the manager."""
    mgr = _make_manager(tmp_path)
    await mgr.register(
        chain_id="run-6", depth=0, original_text="p", sender=None,
        requester=Requester(agent_name="worker", session_id="s1"),
    )

    async def _deliver() -> None:
        pass

    popped = await mgr.settle("run-6", on_settle="deliver", deliver=_deliver)

    assert popped is not None
    assert popped.requester.agent_name == "worker"
    assert popped.requester.session_id == "s1"


@pytest.mark.asyncio
async def test_settle_tolerates_a_missing_handle(tmp_path: Path):
    """Tier 2: settle() on an unregistered chain_id still executes the
    disposition (mirrors resolve()'s ``pop(id, None)`` — no error on a
    missing entry) — a pre-existing run from before this mechanism landed
    must not crash at settle."""
    mgr = _make_manager(tmp_path)
    calls = []

    async def _deliver() -> None:
        calls.append("delivered")

    popped = await mgr.settle("never-registered", on_settle="deliver", deliver=_deliver)

    assert popped is None
    assert calls == ["delivered"]
