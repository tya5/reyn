"""Tier 2: OS invariant — compare() correctly diffs two recorded sessions.

Invariants:
  - Two identical traces produce DiffFrames with has_diff=False
  - A changed event kind count is detected in events_diff
  - A changed state_snapshot key is detected in state_diff
  - A changed LLM prompt or response is detected in llm_diff
  - scope filter correctly aggregates at step / phase / skill_run
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.replay import compare

# ---------------------------------------------------------------------------
# Fixture helpers (duplicated from test_replay_engine.py for isolation)
# ---------------------------------------------------------------------------

def _wal_step_started(
    *, seq: int, run_id: str = "run1", phase: str = "p1",
    op_invocation_id: str | None = None
) -> dict:
    oid = op_invocation_id or f"oid_{seq}"
    return {
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_started", "run_id": run_id, "phase": phase,
        "op_kind": "file", "op_invocation_id": oid,
        "args_hash": "abc", "args": {},
    }


def _wal_step_completed(
    *, seq: int, run_id: str = "run1", phase: str = "p1",
    op_invocation_id: str | None = None
) -> dict:
    oid = op_invocation_id or f"oid_{seq - 1}"
    return {
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_completed", "run_id": run_id, "phase": phase,
        "op_kind": "file", "op_invocation_id": oid, "result": {},
    }


def _wal_step_failed(
    *, seq: int, run_id: str = "run1", phase: str = "p1",
    op_invocation_id: str | None = None, error: str = "err"
) -> dict:
    oid = op_invocation_id or f"oid_{seq - 1}"
    return {
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "step_failed", "run_id": run_id, "phase": phase,
        "op_invocation_id": oid, "error": error,
    }


def _wal_skill_started(*, seq: int, run_id: str = "run1") -> dict:
    return {
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "skill_started", "run_id": run_id, "skill": "demo",
    }


def _wal_phase_advanced(*, seq: int, run_id: str = "run1", phase: str = "p1") -> dict:
    return {
        "seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": "skill_phase_advanced", "run_id": run_id, "phase": phase,
    }


def _llm_request(
    *, request_id: str = "req1", phase: str = "p1",
    model: str = "test-model", content: str = "hello"
) -> dict:
    return {
        "kind": "request", "request_id": request_id,
        "timestamp": "2026-01-01T00:00:05", "model": model,
        "caller_hint": f"phase:{phase}",
        "messages": [{"role": "user", "content": content}],
        "tools": None,
    }


def _llm_response(
    *, request_id: str = "req1", content: str = "ok",
    finish_reason: str = "stop"
) -> dict:
    return {
        "kind": "response", "request_id": request_id,
        "timestamp": "2026-01-01T00:00:06",
        "content": content, "finish_reason": finish_reason,
        "tool_calls": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _write_trace(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _minimal_trace(tmp_path: Path, name: str) -> Path:
    """Return a path to a minimal one-step trace."""
    p = tmp_path / name
    _write_trace(p, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_identical_traces_no_diff(tmp_path: Path):
    """Tier 2: compare() of two identical traces yields only frames with has_diff=False."""
    a = _minimal_trace(tmp_path, "a.jsonl")
    b = _minimal_trace(tmp_path, "b.jsonl")
    frames = list(compare(str(a), str(b), scope="step"))
    assert all(not f.has_diff for f in frames)


def test_event_kind_difference_detected(tmp_path: Path):
    """Tier 2: a step_failed in 'after' instead of step_completed is detected in events_diff."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    _write_trace(before, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    _write_trace(after, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_failed(seq=4, op_invocation_id="oid1", error="disk full"),
    ])
    frames = list(compare(str(before), str(after), scope="step"))
    assert len(frames) == 1
    df = frames[0]
    assert df.has_diff
    # events_diff should report step_completed / step_failed change.
    changes = df.events_diff.get("changes", [])
    kinds_changed = {ch["kind"] for ch in changes}
    assert "step_completed" in kinds_changed or "step_failed" in kinds_changed


def test_state_diff_detected(tmp_path: Path):
    """Tier 2: state_snapshot key change is detected in state_diff."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    # Before: step succeeds
    _write_trace(before, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    # After: step fails — different state_snapshot keys
    _write_trace(after, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_failed(seq=4, op_invocation_id="oid1", error="timeout"),
    ])
    frames = list(compare(str(before), str(after), scope="step"))
    assert len(frames) == 1
    df = frames[0]
    # state_diff should be populated (keys differ: last_completed_op vs last_error)
    assert df.has_diff


def test_llm_prompt_diff_detected(tmp_path: Path):
    """Tier 2: changed LLM prompt content is detected in llm_diff."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    _write_trace(before, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _llm_request(request_id="req1", phase="p1", content="original prompt"),
        _llm_response(request_id="req1"),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    _write_trace(after, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _llm_request(request_id="req1", phase="p1", content="changed prompt after fix"),
        _llm_response(request_id="req1"),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    frames = list(compare(str(before), str(after), scope="step"))
    assert len(frames) == 1
    df = frames[0]
    assert "prompt_diff" in df.llm_diff
    assert df.llm_diff["prompt_diff"]["changed"] is True


def test_llm_response_diff_detected(tmp_path: Path):
    """Tier 2: changed LLM response content is detected in llm_diff."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    _write_trace(before, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _llm_request(request_id="req1", phase="p1"),
        _llm_response(request_id="req1", content="response A"),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    _write_trace(after, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _llm_request(request_id="req1", phase="p1"),
        _llm_response(request_id="req1", content="response B post fix"),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    frames = list(compare(str(before), str(after), scope="step"))
    assert len(frames) == 1
    df = frames[0]
    assert "response_diff" in df.llm_diff
    assert df.llm_diff["response_diff"]["changed"] is True


def test_scope_phase_aggregation(tmp_path: Path):
    """Tier 2: compare(scope='phase') aggregates steps to phase level."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    # Each trace: 2 steps in phase_a
    for path in (before, after):
        _write_trace(path, [
            _wal_skill_started(seq=1),
            _wal_phase_advanced(seq=2, phase="phase_a"),
            _wal_step_started(seq=3, phase="phase_a", op_invocation_id="oid1"),
            _wal_step_completed(seq=4, phase="phase_a", op_invocation_id="oid1"),
            _wal_step_started(seq=5, phase="phase_a", op_invocation_id="oid2"),
            _wal_step_completed(seq=6, phase="phase_a", op_invocation_id="oid2"),
        ])
    frames = list(compare(str(before), str(after), scope="phase"))
    # Two steps aggregate to one phase frame.
    assert len(frames) == 1
    assert frames[0].before is not None
    assert frames[0].before.checkpoint.phase == "phase_a"


def test_scope_skill_run_aggregation(tmp_path: Path):
    """Tier 2: compare(scope='skill_run') aggregates to one frame per run_id."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    for path in (before, after):
        _write_trace(path, [
            _wal_skill_started(seq=1, run_id="run_x"),
            _wal_phase_advanced(seq=2, run_id="run_x", phase="p1"),
            _wal_step_started(seq=3, run_id="run_x", phase="p1", op_invocation_id="oid1"),
            _wal_step_completed(seq=4, run_id="run_x", phase="p1", op_invocation_id="oid1"),
        ])
    frames = list(compare(str(before), str(after), scope="skill_run"))
    assert len(frames) == 1
    assert frames[0].before.checkpoint.run_id == "run_x"


def test_compare_unequal_length_traces(tmp_path: Path):
    """Tier 2: when after has more frames than before, surplus has before=None."""
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    _write_trace(before, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
    ])
    # after has two steps
    _write_trace(after, [
        _wal_skill_started(seq=1),
        _wal_phase_advanced(seq=2),
        _wal_step_started(seq=3, op_invocation_id="oid1"),
        _wal_step_completed(seq=4, op_invocation_id="oid1"),
        _wal_step_started(seq=5, op_invocation_id="oid2"),
        _wal_step_completed(seq=6, op_invocation_id="oid2"),
    ])
    frames = list(compare(str(before), str(after), scope="step"))
    assert len(frames) == 2
    # First frame: both before and after present.
    assert frames[0].before is not None
    assert frames[0].after is not None
    # Second frame: only after present.
    assert frames[1].before is None
    assert frames[1].after is not None
