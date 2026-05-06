"""Tier 2: OS invariant — ReplayEngine correctly walks, seeks, and aggregates
recorded WAL + LLM trace data.

The engine is the read-only foundation for debug time-travel.  Invariants:
  - walk(scope="step") yields one StepFrame per step_started/completed pair
  - seek(checkpoint) returns exactly the matching frame or raises KeyError
  - list_checkpoints() returns Checkpoints consistent with walk()
  - empty / malformed trace files produce graceful results (no crash)
  - multi-source (WAL + LLM trace) records are merged correctly
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.replay import Checkpoint, ReplayEngine, StepFrame


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _wal_step_started(
    *,
    seq: int,
    run_id: str = "run1",
    phase: str = "p1",
    op_kind: str = "file",
    op_invocation_id: str | None = None,
) -> dict:
    oid = op_invocation_id or f"oid_{seq}"
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_started",
        "run_id": run_id,
        "phase": phase,
        "op_kind": op_kind,
        "op_invocation_id": oid,
        "args_hash": "abc",
        "args": {"op": "write"},
    }


def _wal_step_completed(
    *,
    seq: int,
    run_id: str = "run1",
    phase: str = "p1",
    op_kind: str = "file",
    op_invocation_id: str | None = None,
    result: dict | None = None,
) -> dict:
    oid = op_invocation_id or f"oid_{seq - 1}"
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_completed",
        "run_id": run_id,
        "phase": phase,
        "op_kind": op_kind,
        "op_invocation_id": oid,
        "result": result or {},
    }


def _wal_step_failed(
    *,
    seq: int,
    run_id: str = "run1",
    phase: str = "p1",
    op_invocation_id: str | None = None,
    error: str = "oops",
) -> dict:
    oid = op_invocation_id or f"oid_{seq - 1}"
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_failed",
        "run_id": run_id,
        "phase": phase,
        "op_invocation_id": oid,
        "error": error,
    }


def _wal_skill_started(
    *, seq: int, run_id: str = "run1", skill: str = "demo"
) -> dict:
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "skill_started",
        "run_id": run_id,
        "skill": skill,
    }


def _wal_phase_advanced(
    *, seq: int, run_id: str = "run1", phase: str = "p1"
) -> dict:
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "skill_phase_advanced",
        "run_id": run_id,
        "phase": phase,
    }


def _wal_skill_completed(*, seq: int, run_id: str = "run1") -> dict:
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "skill_completed",
        "run_id": run_id,
    }


def _llm_request(
    *,
    request_id: str = "req1",
    phase: str = "p1",
    model: str = "test-model",
    messages: list | None = None,
) -> dict:
    return {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:05",
        "model": model,
        "caller_hint": f"phase:{phase}",
        "messages": messages or [{"role": "user", "content": "hello"}],
        "tools": None,
    }


def _llm_response(
    *, request_id: str = "req1", content: str = "ok", finish_reason: str = "stop"
) -> dict:
    return {
        "kind": "response",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:06",
        "content": content,
        "finish_reason": finish_reason,
        "tool_calls": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _write_trace(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Tests: walk
# ---------------------------------------------------------------------------

def test_walk_single_step_yields_one_frame(tmp_path: Path):
    """Tier 2: walk() over a trace with one step returns exactly one StepFrame."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, StepFrame)
    assert frame.checkpoint.run_id == "run1"
    assert frame.checkpoint.phase == "p1"
    assert frame.checkpoint.step_idx == 0


def test_walk_two_steps_same_phase(tmp_path: Path):
    """Tier 2: two steps in the same phase produce step_idx 0 and 1."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
        _wal_step_started(seq=5, op_invocation_id="oid2"),
        _wal_step_completed(seq=6, op_invocation_id="oid2"),
    ])
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 2
    assert frames[0].checkpoint.step_idx == 0
    assert frames[1].checkpoint.step_idx == 1


def test_walk_step_failed_produces_frame(tmp_path: Path):
    """Tier 2: step_failed closes the step and creates a StepFrame."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_failed(seq=4, op_invocation_id="oid1", error="disk full"),
    ])
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 1
    snap = frames[0].state_snapshot
    assert snap.get("last_error") == "disk full"


def test_walk_events_attached_to_frame(tmp_path: Path):
    """Tier 2: events list in StepFrame contains the step events."""
    trace = tmp_path / "trace.jsonl"
    records = [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ]
    _write_trace(trace, records)
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 1
    kinds = [ev.get("kind") for ev in frames[0].events]
    assert "step_started" in kinds
    assert "step_completed" in kinds


def test_walk_multi_source_merge(tmp_path: Path):
    """Tier 2: WAL events and LLM trace records in one file are split correctly."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _llm_request(request_id="req1", phase="p1"),
        _llm_response(request_id="req1"),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 1
    # LLM payload should be attached.
    assert frames[0].llm_payload is not None
    assert frames[0].llm_payload.get("model") == "test-model"
    assert frames[0].llm_result is not None
    assert frames[0].llm_result.get("finish_reason") == "stop"


# ---------------------------------------------------------------------------
# Tests: seek
# ---------------------------------------------------------------------------

def test_seek_returns_correct_frame(tmp_path: Path):
    """Tier 2: seek(checkpoint) returns the StepFrame matching that checkpoint."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
        _wal_step_started(seq=5, op_invocation_id="oid2"),
        _wal_step_completed(seq=6, op_invocation_id="oid2"),
    ])
    engine = ReplayEngine(str(trace))
    cp = Checkpoint(run_id="run1", phase="p1", step_idx=1)
    frame = engine.seek(cp)
    assert frame.checkpoint == cp


def test_seek_raises_key_error_for_missing_checkpoint(tmp_path: Path):
    """Tier 2: seek() raises KeyError when the checkpoint does not exist."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    engine = ReplayEngine(str(trace))
    cp = Checkpoint(run_id="run1", phase="p1", step_idx=99)
    with pytest.raises(KeyError):
        engine.seek(cp)


# ---------------------------------------------------------------------------
# Tests: list_checkpoints
# ---------------------------------------------------------------------------

def test_list_checkpoints_step_scope(tmp_path: Path):
    """Tier 2: list_checkpoints(scope='step') matches walk(scope='step')."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
        _wal_step_started(seq=5, op_invocation_id="oid2"),
        _wal_step_completed(seq=6, op_invocation_id="oid2"),
    ])
    engine = ReplayEngine(str(trace))
    cps = engine.list_checkpoints(scope="step")
    assert len(cps) == 2
    assert cps[0] == Checkpoint(run_id="run1", phase="p1", step_idx=0)
    assert cps[1] == Checkpoint(run_id="run1", phase="p1", step_idx=1)


def test_list_checkpoints_phase_scope(tmp_path: Path):
    """Tier 2: list_checkpoints(scope='phase') returns one per unique phase."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2, phase="phase_a"),
        _wal_step_started(seq=3, phase="phase_a", op_invocation_id="oid1"),
        _wal_step_completed(seq=4, phase="phase_a", op_invocation_id="oid1"),
        _wal_phase_advanced(seq=5, phase="phase_b"),
        _wal_step_started(seq=6, phase="phase_b", op_invocation_id="oid2"),
        _wal_step_completed(seq=7, phase="phase_b", op_invocation_id="oid2"),
    ])
    engine = ReplayEngine(str(trace))
    cps = engine.list_checkpoints(scope="phase")
    phases = [cp.phase for cp in cps]
    assert "phase_a" in phases
    assert "phase_b" in phases
    # Each phase should appear only once.
    assert len(phases) == len(set(phases))


def test_list_checkpoints_skill_run_scope(tmp_path: Path):
    """Tier 2: list_checkpoints(scope='skill_run') returns one per run_id."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1, run_id="run_a"),
        _wal_phase_advanced(seq=2, run_id="run_a", phase="p1"),
        _wal_step_started(seq=3, run_id="run_a", phase="p1", op_invocation_id="oid1"),
        _wal_step_completed(seq=4, run_id="run_a", phase="p1", op_invocation_id="oid1"),
    ])
    engine = ReplayEngine(str(trace))
    cps = engine.list_checkpoints(scope="skill_run")
    assert len(cps) == 1
    assert cps[0].run_id == "run_a"


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------

def test_empty_trace_walk_returns_no_frames(tmp_path: Path):
    """Tier 2: empty trace file yields no StepFrames without error."""
    trace = tmp_path / "empty.jsonl"
    trace.write_text("", encoding="utf-8")
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert frames == []


def test_malformed_lines_are_skipped(tmp_path: Path):
    """Tier 2: invalid JSON lines in the trace are skipped silently."""
    trace = tmp_path / "bad.jsonl"
    trace.write_text(
        'not json\n'
        + json.dumps(_wal_skill_started(seq=1)) + "\n"
        + "{broken json\n"
        + json.dumps(_wal_phase_advanced(seq=2)) + "\n"
        + json.dumps(_wal_step_started(seq=3, op_invocation_id="oid1")) + "\n"
        + json.dumps(_wal_step_completed(seq=4, op_invocation_id="oid1")) + "\n",
        encoding="utf-8",
    )
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    # Should parse the valid lines and produce one frame.
    assert len(frames) == 1


def test_missing_trace_raises_file_not_found():
    """Tier 2: ReplayEngine raises FileNotFoundError for a missing file."""
    with pytest.raises(FileNotFoundError):
        ReplayEngine("/tmp/__nonexistent_reyn_trace__.jsonl")


def test_walk_scope_invalid_raises_value_error(tmp_path: Path):
    """Tier 2: walk() raises ValueError for unknown scope."""
    trace = tmp_path / "trace.jsonl"
    trace.write_text("", encoding="utf-8")
    engine = ReplayEngine(str(trace))
    with pytest.raises(ValueError, match="unknown scope"):
        list(engine.walk(scope="bogus"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: multi-file input (PR-TIME-TRAVEL friction follow-up)
#
# Real Reyn sessions split records across two files (.reyn/state/wal.jsonl
# for WAL, REYN_LLM_TRACE_DUMP path for LLM trace).  The engine must accept
# a list of paths so callers don't have to ``cat`` them together.
# ---------------------------------------------------------------------------

def test_init_with_list_of_paths_merges_records(tmp_path: Path):
    """Tier 2: ReplayEngine([wal_path, llm_path]) merges WAL + LLM records."""
    wal_path = tmp_path / "wal.jsonl"
    llm_path = tmp_path / "llm_trace.jsonl"
    _write_trace(wal_path, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    _write_trace(llm_path, [
        _llm_request(request_id="req1", phase="p1"),
        _llm_response(request_id="req1", content="ok"),
    ])
    engine = ReplayEngine([str(wal_path), str(llm_path)])
    frames = list(engine.walk())
    assert len(frames) == 1
    # LLM payload should be attached because phase matches caller_hint.
    assert frames[0].llm_payload is not None
    assert frames[0].llm_result is not None


def test_multi_file_equivalent_to_concatenated_single_file(tmp_path: Path):
    """Tier 2: Loading [wal, llm] is equivalent to loading their concat."""
    wal_records = [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ]
    llm_records = [
        _llm_request(request_id="req1", phase="p1"),
        _llm_response(request_id="req1", content="ok"),
    ]

    # Multi-file load.
    wal_path = tmp_path / "wal.jsonl"
    llm_path = tmp_path / "llm.jsonl"
    _write_trace(wal_path, wal_records)
    _write_trace(llm_path, llm_records)
    multi = list(ReplayEngine([str(wal_path), str(llm_path)]).walk())

    # Concatenated single-file load.
    concat_path = tmp_path / "concat.jsonl"
    _write_trace(concat_path, wal_records + llm_records)
    single = list(ReplayEngine(str(concat_path)).walk())

    # Same number of frames, same checkpoints, same LLM attachment.
    assert len(multi) == len(single) == 1
    assert multi[0].checkpoint == single[0].checkpoint
    assert (multi[0].llm_payload is not None) == (single[0].llm_payload is not None)
    assert (multi[0].llm_result is not None) == (single[0].llm_result is not None)


def test_init_with_empty_list_raises_value_error():
    """Tier 2: ReplayEngine([]) raises ValueError (no paths to load)."""
    with pytest.raises(ValueError, match="non-empty"):
        ReplayEngine([])


def test_init_with_list_includes_missing_file_raises(tmp_path: Path):
    """Tier 2: One missing path in the list raises FileNotFoundError."""
    existing = tmp_path / "wal.jsonl"
    _write_trace(existing, [_wal_skill_started(seq=1)])
    missing = tmp_path / "__does_not_exist__.jsonl"
    with pytest.raises(FileNotFoundError):
        ReplayEngine([str(existing), str(missing)])


def test_init_with_single_string_still_works(tmp_path: Path):
    """Tier 2: Backward compat — single-string path is unchanged."""
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    engine = ReplayEngine(str(trace))
    frames = list(engine.walk())
    assert len(frames) == 1
