"""Tier 2: control_ir_results per-result size cap invariants (FP-0008 PR-N v8).

The size-cap fix adds MAX_CONTROL_IR_RESULT_BYTES = 8_192.  Results whose
string-valued content fields (content / stdout / stderr / output) exceed the
cap are truncated with a structured marker; results below the cap pass through
unchanged.  A ``control_ir_result_truncated`` observability event is emitted
for every truncated result (P6 audit guarantee).

Test coverage:
  1. small result (< cap) → passed through unchanged, no event
  2. large result (> cap) → content field truncated + marker present
  3. truncation marker contains size signal (original byte count, cap)
  4. observability event emitted with correct fields
  5. result metadata fields (kind, status, path) survive truncation intact
  6. cap value is tunable via exported constant
  7. cumulative prompt size for N=10 turns with large results stays bounded
  8. build_frame wires cap for every result in control_ir_results
  9. result with no large-content fields passes through unchanged
 10. multiple large fields in one result (stdout + stderr) both truncated
"""
from __future__ import annotations

import json

from reyn.context_builder import (
    MAX_CONTROL_IR_RESULT_BYTES,
    build_frame,
    cap_control_ir_result,
)
from reyn.events.events import EventLog
from reyn.schemas.models import (
    CandidateOutput,
    Phase,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _large_content(extra_bytes: int = 1000) -> str:
    """Return a string whose UTF-8 encoding exceeds MAX_CONTROL_IR_RESULT_BYTES."""
    target = MAX_CONTROL_IR_RESULT_BYTES + extra_bytes
    return "A" * target


def _small_content(under_bytes: int = 500) -> str:
    """Return a string whose UTF-8 encoding is below MAX_CONTROL_IR_RESULT_BYTES."""
    target = MAX_CONTROL_IR_RESULT_BYTES - under_bytes
    return "B" * max(0, target)


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _minimal_phase() -> Phase:
    return Phase(
        name="test_phase",
        role="test",
        instructions="test instructions",
        input_schema={},
        allowed_ops=[],
    )


def _minimal_candidate(next_phase: str = "end") -> CandidateOutput:
    return CandidateOutput(
        next_phase=next_phase,
        description="finish",
        schema_name="test_artifact",
        artifact_schema={},
        control_type="finish",
    )


# ── 1. small result passes through unchanged ─────────────────────────────────


def test_small_result_passes_through_unchanged() -> None:
    """Tier 2: result with content below cap is returned as-is (identity check)."""
    result = {"kind": "file", "op": "read", "status": "ok", "content": _small_content()}
    capped = cap_control_ir_result(result, 0)
    assert capped is result


def test_small_result_emits_no_event() -> None:
    """Tier 2: small result (below cap) does not emit a control_ir_result_truncated event."""
    events = EventLog()
    result = {"kind": "file", "op": "read", "status": "ok", "content": _small_content()}
    cap_control_ir_result(result, 0, events=events, phase="p")
    trunc_events = [e for e in events.all() if e.type == "control_ir_result_truncated"]
    assert trunc_events == []


# ── 2. large result is truncated + marker present ─────────────────────────────


def test_large_content_field_is_truncated() -> None:
    """Tier 2: content field exceeding cap is replaced with a truncation marker."""
    large = _large_content()
    result = {"kind": "file", "op": "read", "status": "ok", "content": large}
    capped = cap_control_ir_result(result, 0)

    capped_content = capped["content"]
    # The marker, not the original content, should be present.
    assert "<truncated:" in capped_content
    # The capped content string is shorter than the original.
    assert _byte_len(capped_content) < _byte_len(large)


def test_large_stdout_field_is_truncated() -> None:
    """Tier 2: stdout field (shell/sandboxed_exec result) exceeding cap is truncated."""
    large = _large_content()
    result = {"kind": "shell", "status": "ok", "returncode": 0, "stdout": large, "stderr": ""}
    capped = cap_control_ir_result(result, 0)
    assert "<truncated:" in capped["stdout"]


# ── 3. truncation marker contains size signal ─────────────────────────────────


def test_truncation_marker_contains_original_byte_count() -> None:
    """Tier 2: truncation marker carries original byte count so the LLM knows the omission size."""
    large = _large_content(extra_bytes=5000)
    original_bytes = _byte_len(large)
    result = {"kind": "file", "op": "read", "status": "ok", "content": large}
    capped = cap_control_ir_result(result, 0)

    marker = capped["content"]
    assert str(original_bytes) in marker


def test_truncation_marker_contains_cap_value() -> None:
    """Tier 2: truncation marker includes the cap value for transparency."""
    large = _large_content()
    result = {"kind": "file", "op": "read", "status": "ok", "content": large}
    capped = cap_control_ir_result(result, 0)
    assert str(MAX_CONTROL_IR_RESULT_BYTES) in capped["content"]


# ── 4. observability event emitted ────────────────────────────────────────────


def test_truncation_emits_observability_event() -> None:
    """Tier 2: truncated result emits control_ir_result_truncated event with required fields."""
    events = EventLog()
    large = _large_content()
    original_bytes = _byte_len(large)
    result = {"kind": "file", "op": "read", "status": "ok", "content": large}
    cap_control_ir_result(
        result, 3,
        events=events, phase="act_phase", run_id="run-1", turn=5,
    )

    trunc_events = [e for e in events.all() if e.type == "control_ir_result_truncated"]
    (ev,) = trunc_events  # exactly one event
    assert ev.data["phase"] == "act_phase"
    assert ev.data["run_id"] == "run-1"
    assert ev.data["turn"] == 5
    assert ev.data["result_idx"] == 3
    assert ev.data["result_kind"] == "file"
    assert ev.data["cap_bytes"] == MAX_CONTROL_IR_RESULT_BYTES
    assert "content" in ev.data["truncated_fields"]
    assert ev.data["truncated_fields"]["content"] == original_bytes


# ── 5. metadata fields survive truncation intact ──────────────────────────────


def test_metadata_fields_survive_truncation() -> None:
    """Tier 2: kind / status / path / op fields are not modified by truncation."""
    result = {
        "kind": "file",
        "op": "read",
        "path": "src/foo.py",
        "status": "ok",
        "content": _large_content(),
    }
    capped = cap_control_ir_result(result, 0)
    assert capped["kind"] == "file"
    assert capped["op"] == "read"
    assert capped["path"] == "src/foo.py"
    assert capped["status"] == "ok"


# ── 6. cap value is tunable via exported constant ─────────────────────────────


def test_cap_constant_is_exported_and_numeric() -> None:
    """Tier 2: MAX_CONTROL_IR_RESULT_BYTES is an exported numeric constant (future tuning)."""
    assert isinstance(MAX_CONTROL_IR_RESULT_BYTES, int)
    assert MAX_CONTROL_IR_RESULT_BYTES > 0


# ── 7. cumulative prompt size is bounded over N=10 turns ─────────────────────


def test_cumulative_prompt_size_bounded_over_10_turns() -> None:
    """Tier 2: after 10 turns of accumulation, total capped-results JSON stays bounded.

    Each turn adds one file_read result with a large content field.  Without
    the cap the total would reach ~14× the per-result raw size.  With the cap,
    total JSON of all accumulated results stays under a reasonable bound.
    """
    # Simulate 10 turns each adding one 100 KB result.
    large = "X" * 100_000  # ~100 KB per result
    accumulated: list[dict] = []
    events = EventLog()

    for turn in range(10):
        raw_result = {
            "kind": "file", "op": "read", "status": "ok",
            "path": f"file_{turn}.py", "content": large,
        }
        capped = cap_control_ir_result(raw_result, turn, events=events, phase="p", turn=turn)
        accumulated.append(capped)

    total_json_bytes = len(json.dumps(accumulated, ensure_ascii=False).encode("utf-8"))
    # Bounded: each result's content is capped; 10 results should be well
    # under 500 KB (without cap they'd be ~1 MB).
    upper_bound = 500_000
    assert total_json_bytes < upper_bound, (
        f"Accumulated results too large: {total_json_bytes} bytes >= {upper_bound}"
    )

    # Every turn should have emitted at least one truncation event.
    trunc_events = [e for e in events.all() if e.type == "control_ir_result_truncated"]
    assert len(trunc_events) > 0


# ── 8. build_frame wires cap for every result ─────────────────────────────────


def test_build_frame_applies_cap_to_control_ir_results() -> None:
    """Tier 2: build_frame applies cap_control_ir_result to every entry in control_ir_results."""
    events = EventLog()
    large = _large_content()
    results = [
        {"kind": "file", "op": "read", "status": "ok", "content": large},
        {"kind": "file", "op": "read", "status": "ok", "content": large},
    ]
    phase = _minimal_phase()
    candidate = _minimal_candidate()

    frame = build_frame(
        phase_name="test_phase",
        phase=phase,
        artifact={"type": "test_artifact", "data": {}},
        candidates=[candidate],
        output_language="en",
        history=[],
        visit_counts={},
        finish_criteria=["done"],
        max_phase_visits=None,
        available_ops=[],
        effective_model="test-model",
        model_resolved="test-model-resolved",
        events=events,
        control_ir_results=results,
    )

    # Both results in the frame should be truncated.
    frame_results = frame.control_ir_results
    for r in frame_results:
        assert "<truncated:" in r["content"]

    # At least one truncation event should have been emitted (one per truncated result).
    trunc_events = [e for e in events.all() if e.type == "control_ir_result_truncated"]
    assert len(trunc_events) > 0


# ── 9. result with no large-content fields passes unchanged ───────────────────


def test_result_with_no_large_fields_passes_unchanged() -> None:
    """Tier 2: result with only metadata (no content/stdout/stderr/output) is not modified."""
    result = {
        "kind": "file",
        "op": "write",
        "path": "out.txt",
        "status": "ok",
    }
    capped = cap_control_ir_result(result, 0)
    assert capped is result


# ── 10. multiple large fields in one result both truncated ────────────────────


def test_multiple_large_fields_both_truncated() -> None:
    """Tier 2: result with large stdout AND large stderr has both fields truncated."""
    large = _large_content()
    result = {
        "kind": "shell",
        "status": "error",
        "returncode": 1,
        "stdout": large,
        "stderr": large,
    }
    events = EventLog()
    capped = cap_control_ir_result(result, 0, events=events, phase="p")

    assert "<truncated:" in capped["stdout"]
    assert "<truncated:" in capped["stderr"]

    # One truncation event per result (both fields listed in truncated_fields of the same event).
    trunc_events = [e for e in events.all() if e.type == "control_ir_result_truncated"]
    (ev,) = trunc_events  # exactly one event (both fields folded into a single event)
    assert "stdout" in ev.data["truncated_fields"]
    assert "stderr" in ev.data["truncated_fields"]
