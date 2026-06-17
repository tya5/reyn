"""Tier 2: FP-0008 #1135(b) — phase_output_validation_failed event captures raw LLM output.

When a phase output fails validation, the model's raw emitted output was only in
the opt-in REYN_LLM_TRACE_DUMP — never in the always-on P6 events log (phase_failed
/ validation_error carried only the error string). This made failures like the
decide-turn "missing control block" undiagnosable from events alone.

The additive `phase_output_validation_failed` event (canonical contract in #1135)
carries the raw output: inline when ≤ cap, else a **state_dir-relative** offload
handle. Existing validation events are unchanged (TUI/test back-compat).

These pin the new contract behaviorally: inline vs offload by size, the ref is
state_dir-relative (not absolute) and round-trips via read_offloaded, exactly-one
of raw_output/raw_output_ref, failure_kind, and a real validation-failure path
emits the event alongside the unchanged kind-specific one.

Real EventLog / Workspace / ControlIRExecutor; no mocks. Docstring opens "Tier 2:".
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.kernel.phase_executor import _RAW_OUTPUT_INLINE_CAP, PhaseExecutor
from reyn.data.workspace.workspace import Workspace
from reyn.services.offload import read_offloaded


def _executor(tmp_path: Path) -> tuple[PhaseExecutor, EventLog, Workspace]:
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path, state_dir=tmp_path / "state")
    cie = ControlIRExecutor(
        workspace=ws, events=events, permission_resolver=None, skill_name="t",
    )
    # Only control_ir_executor (for workspace.state_dir) + events are exercised by
    # the capture helper; the rest are inert stores.
    px = PhaseExecutor(
        llm_caller=None, control_ir_executor=cie, events=events, skill=None,
        safety=None, intervention_bus=None, build_frame_fn=None,
    )
    return px, events, ws


def _last_pov_event(events: EventLog) -> dict:
    evs = [e for e in events.all() if e.type == "phase_output_validation_failed"]
    assert evs, "expected a phase_output_validation_failed event"
    return evs[-1].data


def test_small_raw_is_inline(tmp_path: Path) -> None:
    """Tier 2: a raw output ≤ cap is captured inline in raw_output, ref is null."""
    px, events, _ws = _executor(tmp_path)
    raw = {"type": "decide", "oops": "no control block"}
    px._emit_output_validation_failed(
        phase="verify", attempt=2, failure_kind="control_ir", error="missing control", raw=raw,
    )
    d = _last_pov_event(events)
    assert d["phase"] == "verify"
    assert d["attempt"] == 2
    assert d["failure_kind"] == "control_ir"
    assert d["error"] == "missing control"
    assert d["raw_output"] == json.dumps(raw, ensure_ascii=False)
    assert d["raw_output_ref"] is None
    # exactly-one invariant
    assert (d["raw_output"] is None) != (d["raw_output_ref"] is None)


def test_large_raw_is_offloaded_with_relative_ref(tmp_path: Path) -> None:
    """Tier 2: a raw output > cap offloads; ref is state_dir-RELATIVE and round-trips.

    This pins the cross-session contract: the write-side stores a relative ref
    (not the absolute offload path) and the read-side dereferences it via
    read_offloaded(state_dir / ref, base_dir=state_dir).
    """
    px, events, ws = _executor(tmp_path)
    big = "x" * (_RAW_OUTPUT_INLINE_CAP + 100)
    raw = {"type": "act", "ops": [{"kind": "sandboxed_exec", "blob": big}]}
    px._emit_output_validation_failed(
        phase="verify", attempt=1, failure_kind="act_ops", error="bad", raw=raw,
    )
    d = _last_pov_event(events)
    assert d["raw_output"] is None
    ref = d["raw_output_ref"]
    assert ref is not None
    # ref must be RELATIVE (not an absolute path)
    assert not Path(ref).is_absolute(), f"ref must be state_dir-relative, got {ref!r}"
    # exactly-one invariant
    assert (d["raw_output"] is None) != (d["raw_output_ref"] is None)
    # read-side contract: read_offloaded(state_dir / ref, base_dir=state_dir) round-trips.
    recovered, found = read_offloaded(str(ws.state_dir / ref), base_dir=ws.state_dir)
    assert found is True
    assert json.loads(recovered) == raw
    # the offloaded file lives under the control_ir_offload root.
    assert ref.startswith("control_ir_offload")


def test_existing_validation_events_unchanged_and_new_event_additive(tmp_path: Path) -> None:
    """Tier 2: a real normalize failure emits BOTH the unchanged kind-specific event and the new one.

    Drives the real _validate_phase_output path with a control-less raw so
    normalize() rejects it — exercising the additive emit (back-compat: the
    existing control_ir_validation_error / normalization_error event still fires).
    """
    from reyn.core.kernel.run_state import RunState
    from reyn.schemas.models import CandidateOutput

    px, events, _ws = _executor(tmp_path)
    candidates = [
        CandidateOutput(
            next_phase="report", schema_name="r",
            artifact_schema={"type": "object", "properties": {}},
            control_type="transition", description="",
        )
    ]
    state = RunState()
    # A raw with no usable control block → normalize raises → emits a kind-specific
    # validation event AND the additive phase_output_validation_failed.
    bad_raw = {"artifact": {"type": "r", "data": {}}}
    try:
        px._validate_phase_output(
            bad_raw, "verify", candidates, ["report"], state, attempt=0,
        )
    except (ValueError, Exception):
        pass  # validation is expected to reject; we assert on the emitted events

    types = [e.type for e in events.all()]
    assert "phase_output_validation_failed" in types, (
        f"the additive capture event must fire on a validation failure; got {types}"
    )
    # back-compat: a kind-specific validation event still fired (unchanged).
    assert any(
        t in types for t in ("control_ir_validation_error", "normalization_error", "validation_error")
    ), f"existing kind-specific validation event must still fire (back-compat); got {types}"
    d = _last_pov_event(events)
    assert d["failure_kind"] in {"control_ir", "normalization", "artifact_structure",
                                 "artifact_data", "ops_structure", "output_validation"}
    assert d["raw_output"] == json.dumps(bad_raw, ensure_ascii=False)
