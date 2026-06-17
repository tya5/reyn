"""Tier 2: FP-0021 event audit completeness invariant.

Enforces that all events declared in EVENT_AUDIT_REQUIREMENTS carry the
required audit fields when emitted in production code paths.  Validated
by exercising each event kind through its real production code path and
asserting on the emitted record's data dict.

Three tests cover all 8 FP-0021 events:

1. test_workflow_events_carry_audit_fields
   Fires: workflow_started, workflow_finished, llm_called,
          llm_response_received, permission_granted
   Path: OSRuntime.run() with monkeypatched call_llm + execute_op write
         inside .reyn/ (= allowed zone).

2. test_permission_denied_carries_audit_fields
   Fires: permission_denied
   Path: execute_op() with a write op outside the allowed zone.

3. test_intervention_events_carry_audit_fields
   Fires: user_intervention_requested, user_intervention_received
   Path: execute_op() with an ask_user op + a fake InterventionBus.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real EventLog, real PermissionResolver, real execute_op.
- call_llm is replaced with a plain async callable (not AsyncMock).
- Intervention is answered by a plain fake InterventionBus (not a mock).
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.kernel.llm_call_recorder as runtime_mod
from reyn.data.workspace.workspace import Workspace
from reyn.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.events.events import EventLog
from reyn.llm.llm import LLMCallResult
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.schemas.models import (
    AskUserIROp,
    FileIROp,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_RUN_ID = "fp0021_audit_test"
_SKILL_NAME = "audit_test_skill"


def _make_skill() -> Skill:
    """Minimal single-phase finish-capable skill for OSRuntime.run()."""
    phase = Phase(
        name="main",
        instructions="test phase",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name=_SKILL_NAME,
        entry_phase="main",
        phases={"main": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["main"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


class _FinishCallLLM:
    """Plain async callable that returns a deterministic finish response.

    Replaces call_llm in tests — NOT a mock; no AsyncMock/MagicMock.
    """

    async def __call__(self, resolved_spec, frame, *args, **kwargs):  # noqa: ARG002
        return LLMCallResult(
            data={
                "type": "finish",
                "control": {
                    "type": "finish",
                    "decision": "finish",
                    "next_phase": None,
                    "confidence": 1.0,
                    "reason": {"summary": "done"},
                },
                "artifact": {"type": "result", "data": {}},
            },
            usage=None,
        )


def _make_resolver(tmp_path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )


def _make_ctx(
    tmp_path,
    events: EventLog,
    *,
    resolver: PermissionResolver,
    run_id: str | None = None,
    skill_name: str = _SKILL_NAME,
    intervention_bus: "InterventionBus | None" = None,
) -> OpContext:
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        skill_name=skill_name,
        run_id=run_id,
        current_phase="main",
        intervention_bus=intervention_bus,
    )


# ---------------------------------------------------------------------------
# Test 1: workflow lifecycle + LLM events + permission_granted (5 events)
# ---------------------------------------------------------------------------


def test_workflow_events_carry_audit_fields(tmp_path, monkeypatch):
    """Tier 2: FP-0021 — workflow_started, workflow_finished, llm_called,
    llm_response_received, and permission_granted all carry required audit fields.

    Approach:
    - OSRuntime.run() is called with a monkeypatched call_llm that returns a
      finish decision deterministically.  This fires the 4 lifecycle / LLM events.
    - A separate execute_op() call with a write inside .reyn/ (allowed zone)
      fires permission_granted with run_id + skill + phase.
    """
    monkeypatch.chdir(tmp_path)

    # ── Part A: OSRuntime.run() fires workflow_* + llm_* events ────────────
    from reyn.kernel.runtime import OSRuntime

    monkeypatch.setattr(runtime_mod, "call_llm", _FinishCallLLM())

    rt = OSRuntime(
        _make_skill(),
        model="stub/model",
        run_id=_RUN_ID,
    )
    asyncio.run(rt.run({"type": "input", "data": {}}))

    emitted = {e.type: e for e in rt.events.all()}

    workflow_events = ["workflow_started", "workflow_finished", "llm_called", "llm_response_received"]
    for kind in workflow_events:
        assert kind in emitted, f"FP-0021: '{kind}' not emitted during run()"
        ev = emitted[kind]
        required = EVENT_AUDIT_REQUIREMENTS[kind]
        for field in required:
            assert field in ev.data, (
                f"FP-0021: '{kind}' missing required audit field '{field}'. "
                f"data={ev.data!r}"
            )
            assert ev.data[field] is not None, (
                f"FP-0021: '{kind}'.data['{field}'] is None; must be non-null."
            )

    # ── Part B: execute_op write inside .reyn/ fires permission_granted ────
    events2 = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(
        tmp_path, events2, resolver=resolver,
        run_id=_RUN_ID, skill_name=_SKILL_NAME,
    )
    op = FileIROp(kind="file", op="write", path=".reyn/audit_test.txt", content="ok")
    asyncio.run(execute_op(op, ctx, caller="control_ir"))

    granted = [e for e in events2.all() if e.type == "permission_granted"]
    assert granted, "FP-0021: 'permission_granted' not emitted for allowed write op"
    ev = granted[0]
    required = EVENT_AUDIT_REQUIREMENTS["permission_granted"]
    for field in required:
        assert field in ev.data, (
            f"FP-0021: 'permission_granted' missing required audit field '{field}'. "
            f"data={ev.data!r}"
        )
        assert ev.data[field] is not None, (
            f"FP-0021: 'permission_granted'.data['{field}'] is None; must be non-null."
        )


# ---------------------------------------------------------------------------
# Test 2: permission_denied carries audit fields
# ---------------------------------------------------------------------------


def test_permission_denied_carries_audit_fields(tmp_path, monkeypatch):
    """Tier 2: FP-0021 — permission_denied carries required audit fields
    run_id, skill, and phase.

    Approach: execute_op() with a write op targeting an absolute path
    outside the allowed zone.  The non-interactive PermissionResolver
    denies it.  OpContext is pre-populated with run_id and current_phase.
    """
    monkeypatch.chdir(tmp_path)

    events = EventLog()
    resolver = _make_resolver(tmp_path)
    ctx = _make_ctx(
        tmp_path, events, resolver=resolver,
        run_id=_RUN_ID, skill_name=_SKILL_NAME,
    )

    # Absolute path outside CWD — denied by PermissionResolver.
    target = tmp_path / "outside_zone.txt"
    op = FileIROp(kind="file", op="write", path=str(target), content="data")
    result = asyncio.run(execute_op(op, ctx, caller="control_ir"))

    assert result["status"] == "denied", f"Expected denied, got: {result}"

    denied = [e for e in events.all() if e.type == "permission_denied"]
    assert denied, "FP-0021: 'permission_denied' not emitted"
    ev = denied[0]
    required = EVENT_AUDIT_REQUIREMENTS["permission_denied"]
    for field in required:
        assert field in ev.data, (
            f"FP-0021: 'permission_denied' missing required audit field '{field}'. "
            f"data={ev.data!r}"
        )
        assert ev.data[field] is not None, (
            f"FP-0021: 'permission_denied'.data['{field}'] is None; must be non-null."
        )


# ---------------------------------------------------------------------------
# Test 3: user_intervention_requested and user_intervention_received
# ---------------------------------------------------------------------------


class _AutoAnswerBus:
    """Fake InterventionBus that immediately answers with a preset text.

    Plain fake (not a mock) — satisfies the InterventionBus Protocol.
    """

    def __init__(self, answer_text: str = "test answer") -> None:
        self._answer_text = answer_text

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text=self._answer_text)


def test_intervention_events_carry_audit_fields(tmp_path, monkeypatch):
    """Tier 2: FP-0021 — user_intervention_requested and user_intervention_received
    carry required audit fields run_id, skill, and intervention_id.

    Approach: execute_op() with an AskUserIROp and a fake InterventionBus
    (_AutoAnswerBus) that resolves the intervention immediately without
    blocking.  Both intervention events are emitted in-process.
    """
    monkeypatch.chdir(tmp_path)

    events = EventLog()
    resolver = _make_resolver(tmp_path)
    bus = _AutoAnswerBus("my answer")
    ctx = _make_ctx(
        tmp_path, events, resolver=resolver,
        run_id=_RUN_ID, skill_name=_SKILL_NAME,
        intervention_bus=bus,
    )

    op = AskUserIROp(kind="ask_user", question="What is your name?")
    result = asyncio.run(execute_op(op, ctx, caller="control_ir"))

    assert result["status"] == "ok", f"ask_user op failed: {result}"

    for kind in ("user_intervention_requested", "user_intervention_received"):
        matches = [e for e in events.all() if e.type == kind]
        assert matches, f"FP-0021: '{kind}' not emitted"
        ev = matches[0]
        required = EVENT_AUDIT_REQUIREMENTS[kind]
        for field in required:
            assert field in ev.data, (
                f"FP-0021: '{kind}' missing required audit field '{field}'. "
                f"data={ev.data!r}"
            )
            assert ev.data[field] is not None, (
                f"FP-0021: '{kind}'.data['{field}'] is None; must be non-null."
            )
