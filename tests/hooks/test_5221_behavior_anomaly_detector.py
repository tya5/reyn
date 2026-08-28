"""Tests for #5221 — the behavioral-anomaly-detector.

Architect ruling (issuecomment-5442787554, issue #5221): closed-vocabulary
behavioral anomaly detector, NOT a "prompt-injection detector" — a judge
constrained to op-kind counts/booleans/timestamps can flag that a turn's
OPERATIONS looked unusual; it never reads message text, so it cannot (and
must never claim to) detect injection itself.

Scope discipline pinned by every test below: `turn_end` fires AFTER the
turn, so this can only alert on / gate the NEXT turn — never the one that
carried the anomalous behavior.

Coverage plan
-------------
Tier 1 (contract):
  - the shipped ``behavior_anomaly.yaml`` DSL parses and registers its three
    pipelines (``check``/``escalate``/``skip``) + the ``AnomalyVerdict``
    schema, through the REAL disk-registration loader
    (``reyn.data.pipelines.registry.build_pipeline_registry``) — the same
    path an operator's ``reyn.yaml`` config entry drives.
Tier 2 (OS invariant):
  - ``TurnBehaviorTally``: counts ONLY its closed watch-list, ignores every
    other audit-event kind, and ``snapshot_and_reset`` clears atomically —
    real ``EventLog``, no mocks.
  - ``record_behavior_anomaly_verdict``: emits ``behavior_anomaly_judged``
    with the exact typed fields; rejects a verdict outside the closed
    {clean, suspicious} vocabulary. Real ``EventLog`` via a hand-built
    ``ToolContext`` (the same idiom test_2425 uses for a non-router-loop
    tool-dispatch caller).
  - Full pipeline logic end-to-end: a REAL ``PipelineExecutor.run`` through
    the shipped DSL's THREE pipelines (transform threshold check -> match ->
    escalate/skip), a REAL tool-registry dispatch (``_make_tool_dispatch``,
    so ``record_behavior_anomaly_verdict`` really emits), and a REAL
    ``Session``/``AgentRegistry`` for the judge ``agent`` step (only the LLM
    reply is scripted) — proving BOTH escalation paths: below-threshold
    (judge never runs, no audit-event at all — the third state) and
    above-threshold (judge runs, ``behavior_anomaly_judged`` lands with the
    judge's own verdict).

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.event_schema import AUDIT_EVENT_KINDS
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import PipelineExecutor
from reyn.core.pipeline.parser import parse_pipeline_docs
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.data.pipelines.registry import build_pipeline_registry
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.turn_behavior_tally import SENSITIVE_OP_KINDS, TurnBehaviorTally
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.record_behavior_anomaly_verdict import (
    RECORD_BEHAVIOR_ANOMALY_VERDICT,
)
from reyn.tools.types import ToolContext
from tests._support.agent_session import make_session
from tests._support.events import settle
from tests._support.paths import REPO_ROOT

_DSL_PATH = REPO_ROOT / "src" / "reyn" / "data" / "pipelines" / "behavior_anomaly.yaml"


# ===========================================================================
# Tier 1 — the closed watch-list is a real subset of AUDIT_EVENT_KINDS
# ===========================================================================


def test_sensitive_op_kinds_is_a_subset_of_the_closed_vocabulary() -> None:
    """Tier 2: ``SENSITIVE_OP_KINDS`` is internal (no external boundary
    depends on it — architect review, PR #5414) so this is not a Tier-1
    pin. Its real value is the vacuity guard: ``set() <= X`` is trivially
    true for an empty set, so ``len(...) > 0`` is what actually rules out
    a silently-emptied watch-list — the subset check alone would stay
    green even if every kind were removed."""
    assert SENSITIVE_OP_KINDS <= AUDIT_EVENT_KINDS
    assert len(SENSITIVE_OP_KINDS) > 0


# ===========================================================================
# Tier 1 — disk registration: the shipped DSL file parses + registers
# ===========================================================================


def test_shipped_pipeline_registers_three_pipelines_and_the_schema(
    tmp_path: Path,
) -> None:
    """Tier 1: the REAL disk-registration loader (the same path an operator's
    ``pipelines.entries`` config drives) parses the shipped file into
    ``behavior_anomaly.check`` / ``.escalate`` / ``.skip``, and the
    ``AnomalyVerdict`` schema is registered alongside them."""
    raw_pipelines = {
        "entries": {
            "behavior_anomaly": {"path": str(_DSL_PATH)},
        },
    }
    registry = build_pipeline_registry(raw_pipelines, tmp_path)

    assert set(registry.names()) == {
        "behavior_anomaly.check", "behavior_anomaly.escalate", "behavior_anomaly.skip",
    }
    schema_registry = registry.get_schema_registry("behavior_anomaly.check")
    assert schema_registry is not None
    assert schema_registry.has("AnomalyVerdict")
    schema = schema_registry.get("AnomalyVerdict")
    assert schema["fields"]["verdict"]["values"] == ["clean", "suspicious"]


# ===========================================================================
# Tier 2 — TurnBehaviorTally: real EventLog, closed watch-list, atomic reset
# ===========================================================================


def test_tally_counts_only_the_watched_kinds() -> None:
    """Tier 2: an unwatched kind (e.g. ``turn_started``) never contributes to
    the tally, even though it fires on the same EventLog."""
    events = EventLog()
    tally = TurnBehaviorTally(events)

    events.emit("turn_started", kind="user")  # NOT watched
    events.emit("secret_set", name="x")  # watched
    events.emit("permission_denied", run_id="r", actor="a", phase="")  # watched
    events.emit("secret_set", name="y")  # watched again — same kind

    total, kinds_csv = tally.snapshot_and_reset()
    assert total == 3
    assert kinds_csv == "permission_denied,secret_set"


def test_tally_snapshot_and_reset_clears_atomically() -> None:
    """Tier 2: a second snapshot right after the first sees an empty window
    — the counters actually cleared, not merely returned a copy."""
    events = EventLog()
    tally = TurnBehaviorTally(events)

    events.emit("secret_set", name="x")
    first_total, first_csv = tally.snapshot_and_reset()
    assert (first_total, first_csv) == (1, "secret_set")

    second_total, second_csv = tally.snapshot_and_reset()
    assert (second_total, second_csv) == (0, "")


def test_tally_empty_window_reports_zero_and_empty_csv() -> None:
    """Tier 2: no watched kind fired at all -> (0, "") — the module's own
    asymmetric-trust note applies here too: this is silence, not a
    certificate of a clean turn."""
    events = EventLog()
    tally = TurnBehaviorTally(events)
    assert tally.snapshot_and_reset() == (0, "")


# ===========================================================================
# Tier 2 — record_behavior_anomaly_verdict: the ONLY producer
# ===========================================================================


def _tool_ctx(events: EventLog) -> ToolContext:
    return ToolContext(events=events, permission_resolver=None, workspace=None, caller_kind="router")


async def _wait_for(predicate, *, delay: float = 0.01) -> None:
    """Poll until *predicate* is true. Unbounded per the owner's testing
    policy (docs/deep-dives/contributing/testing.md, ## Time): a running
    event loop dispatches EventLog subscribers via a queued consumer task
    (#4966), not inline — so a subscriber's effect is not necessarily
    visible the instant ``emit``/the handler call returns."""
    while not predicate():
        await asyncio.sleep(delay)


@pytest.mark.asyncio
async def test_record_verdict_emits_the_typed_audit_event() -> None:
    """Tier 2: a valid call emits ``behavior_anomaly_judged`` with exactly
    the three EVENT_AUDIT_REQUIREMENTS fields."""
    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e), kinds={"behavior_anomaly_judged"})

    result = await RECORD_BEHAVIOR_ANOMALY_VERDICT.handler(
        {"verdict": "suspicious", "chain_id": "c-1", "anomalous_op_count": 3},
        _tool_ctx(events),
    )

    assert result["status"] == "ok"
    await _wait_for(lambda: len(captured) > 0)
    event = captured[-1]
    assert event.type == "behavior_anomaly_judged"
    assert event.data["verdict"] == "suspicious"
    assert event.data["chain_id"] == "c-1"
    assert event.data["anomalous_op_count"] == 3


@pytest.mark.asyncio
async def test_record_verdict_rejects_a_value_outside_the_closed_vocabulary() -> None:
    """Tier 2: asymmetric trust enforced structurally — there is no way to
    record e.g. "verified_clean" or any value stronger than the two the
    design allows. No audit-event is emitted on rejection."""
    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e), kinds={"behavior_anomaly_judged"})

    result = await RECORD_BEHAVIOR_ANOMALY_VERDICT.handler(
        {"verdict": "verified_clean", "chain_id": "c-1", "anomalous_op_count": 1},
        _tool_ctx(events),
    )

    await settle(events)
    assert result["status"] == "error"
    assert captured == []


# ===========================================================================
# Tier 2 — full pipeline logic, both branches, real executor + real registry
# ===========================================================================


class _ScriptedAgentReply:
    """Always answers with the same fixed reply — the LLM is incidental to
    what's under test (mirrors test_0060_phase2_f3b's own idiom)."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any):
        from reyn.llm.llm import LLMToolCallResult
        from reyn.llm.pricing import TokenUsage

        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _agent_registry(tmp_path: Path, state_log, scripted: "_ScriptedAgentReply"):
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    holder: dict = {}
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge,
            ),
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True, resolver=resolver,
        )
        s._loop_driver._loop_observer = (
            lambda loop: setattr(loop, "_llm_caller", scripted)
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


def _load_behavior_anomaly_pipelines() -> "tuple[PipelineRegistry, SchemaRegistry]":
    """Parse the shipped DSL file directly (config-agnostic — H4) and
    register its three pipelines under their BARE declared names (matching
    the file's own internal ``call``/``match`` sibling references, which are
    dot-less)."""
    text = _DSL_PATH.read_text(encoding="utf-8")
    schema_registry = SchemaRegistry()
    pipelines = parse_pipeline_docs(text, schema_registry)
    pipeline_registry = PipelineRegistry()
    for pipeline in pipelines:
        pipeline_registry.register(pipeline.name, pipeline, schema_registry)
    return pipeline_registry, schema_registry


@pytest.mark.asyncio
async def test_below_threshold_skips_the_judge_no_audit_event_at_all(tmp_path: Path) -> None:
    """Tier 2: THE third-state proof. Below the escalation threshold, the
    ``escalate`` pipeline (and therefore the judge and the recorder tool)
    never run at all — ``behavior_anomaly_judged`` is NEVER emitted, not
    emitted-as-clean. This is the state a ``turn_settled`` audit-event with
    no matching ``behavior_anomaly_judged`` represents."""
    pipeline_registry, schema_registry = _load_behavior_anomaly_pipelines()
    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e), kinds={"behavior_anomaly_judged"})
    workspace = Workspace(events=events)
    tool_dispatch = _make_tool_dispatch(
        ToolContext(events=events, permission_resolver=None, workspace=workspace, caller_kind="router"),
    )
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply(_json.dumps({"verdict": "suspicious"}))
    registry = _agent_registry(tmp_path, state_log, scripted)

    result = await PipelineExecutor().run(
        pipeline_registry.get("check"),
        {"chain_id": "c-below", "sensitive_op_count": 1, "sensitive_op_kinds_csv": "secret_set"},
        tool_dispatch=tool_dispatch, state_log=state_log, run_id="run-below",
        registry=registry, default_identity="worker",
        schema_registry=schema_registry, pipeline_registry=pipeline_registry,
    )

    await settle(events)
    assert result.named_stores["result"] == "not_escalated"
    assert captured == []  # the judge never ran — no LLM call either
    assert scripted.calls == 0


@pytest.mark.asyncio
async def test_above_threshold_runs_the_judge_and_records_its_verdict(tmp_path: Path) -> None:
    """Tier 2: THE core proof. Above the escalation threshold, the judge
    ``agent`` step actually runs (schema-constrained to ``AnomalyVerdict``,
    zero capabilities) and its verdict lands as a REAL
    ``behavior_anomaly_judged`` audit-event via the REAL tool-registry
    dispatch (not a stand-in) -- proving the whole
    transform -> match -> agent -> tool chain end-to-end."""
    pipeline_registry, schema_registry = _load_behavior_anomaly_pipelines()
    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e), kinds={"behavior_anomaly_judged"})
    workspace = Workspace(events=events)
    tool_dispatch = _make_tool_dispatch(
        ToolContext(events=events, permission_resolver=None, workspace=workspace, caller_kind="router"),
    )
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply(_json.dumps({"verdict": "suspicious"}))
    registry = _agent_registry(tmp_path, state_log, scripted)

    result = await PipelineExecutor().run(
        pipeline_registry.get("check"),
        {
            "chain_id": "c-above", "sensitive_op_count": 5,
            "sensitive_op_kinds_csv": "exec_threat_match,secret_set",
        },
        tool_dispatch=tool_dispatch, state_log=state_log, run_id="run-above",
        registry=registry, default_identity="worker",
        schema_registry=schema_registry, pipeline_registry=pipeline_registry,
    )

    assert result is not None  # the "escalate" callee's final tool-step output threaded out
    await _wait_for(lambda: len(captured) > 0)
    event = captured[-1]
    assert event.type == "behavior_anomaly_judged"
    assert event.data["verdict"] == "suspicious"
    assert event.data["chain_id"] == "c-above"
    assert event.data["anomalous_op_count"] == 5
    assert scripted.calls >= 1  # the judge DID run — unlike the below-threshold path
