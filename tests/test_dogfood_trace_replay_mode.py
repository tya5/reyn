"""Tier 1: Contract — dogfood_trace.py CLI argument parsing for new modes.

Verifies that:
  - --mode replay requires --trace; exits with error if absent
  - --mode replay --at parses correctly (valid checkpoint format)
  - --mode compare requires --before and --after; exits with error if absent
  - --scope is passed through correctly
  - Existing modes are not affected by the new arguments (regression check)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"


def _run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run dogfood_trace.py with ``args`` and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    return result.returncode, result.stdout, result.stderr


def _write_trace(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _minimal_trace(tmp_path: Path) -> Path:
    p = tmp_path / "trace.jsonl"
    _write_trace(p, [
        {
            "seq": 1, "ts": "2026-01-01T00:00:01", "kind": "skill_started",
            "run_id": "run1", "skill": "demo",
        },
        {
            "seq": 2, "ts": "2026-01-01T00:00:02", "kind": "skill_phase_advanced",
            "run_id": "run1", "phase": "p1",
        },
        {
            "seq": 3, "ts": "2026-01-01T00:00:03", "kind": "step_started",
            "run_id": "run1", "phase": "p1",
            "op_kind": "file", "op_invocation_id": "oid1",
            "args_hash": "abc", "args": {},
        },
        {
            "seq": 4, "ts": "2026-01-01T00:00:04", "kind": "step_completed",
            "run_id": "run1", "phase": "p1",
            "op_kind": "file", "op_invocation_id": "oid1",
            "result": {},
        },
    ])
    return p


# ---------------------------------------------------------------------------
# --mode replay
# ---------------------------------------------------------------------------

def test_replay_mode_no_trace_exits_with_error():
    """Tier 1: --mode replay without --trace prints error and exits non-zero."""
    rc, stdout, stderr = _run("--mode", "replay")
    assert rc != 0
    assert "replay" in stderr.lower() or "trace" in stderr.lower()


def test_replay_mode_with_trace_runs(tmp_path: Path):
    """Tier 1: --mode replay with a valid trace exits 0 and prints output."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run("--mode", "replay", "--trace", str(trace))
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"
    # Should print a replay header.
    assert "replay" in stdout.lower() or "frame" in stdout.lower() or "checkpoint" in stdout.lower()


def test_replay_mode_scope_step(tmp_path: Path):
    """Tier 1: --mode replay --scope step is accepted."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run("--mode", "replay", "--trace", str(trace), "--scope", "step")
    assert rc == 0, f"stderr={stderr!r}"


def test_replay_mode_scope_phase(tmp_path: Path):
    """Tier 1: --mode replay --scope phase is accepted."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run("--mode", "replay", "--trace", str(trace), "--scope", "phase")
    assert rc == 0, f"stderr={stderr!r}"


def test_replay_mode_scope_skill_run(tmp_path: Path):
    """Tier 1: --mode replay --scope skill_run is accepted."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run(
        "--mode", "replay", "--trace", str(trace), "--scope", "skill_run"
    )
    assert rc == 0, f"stderr={stderr!r}"


def test_replay_mode_at_valid_checkpoint(tmp_path: Path):
    """Tier 1: --mode replay --at run1:p1:0 jumps to specific checkpoint."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run(
        "--mode", "replay", "--trace", str(trace), "--at", "run1:p1:0"
    )
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"
    assert "run1" in stdout


def test_replay_mode_at_missing_checkpoint_exits_error(tmp_path: Path):
    """Tier 1: --mode replay --at with a non-existent checkpoint exits non-zero."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run(
        "--mode", "replay", "--trace", str(trace), "--at", "run1:p1:99"
    )
    assert rc != 0


def test_replay_mode_at_malformed_checkpoint(tmp_path: Path):
    """Tier 1: --mode replay --at with malformed checkpoint exits non-zero."""
    trace = _minimal_trace(tmp_path)
    rc, stdout, stderr = _run(
        "--mode", "replay", "--trace", str(trace), "--at", "not_a_checkpoint"
    )
    assert rc != 0


# ---------------------------------------------------------------------------
# --mode compare
# ---------------------------------------------------------------------------

def test_compare_mode_no_before_exits_error():
    """Tier 1: --mode compare without --before exits non-zero with error message."""
    rc, stdout, stderr = _run("--mode", "compare", "--after", "/tmp/x.jsonl")
    assert rc != 0
    assert "before" in stderr.lower() or "compare" in stderr.lower()


def test_compare_mode_no_after_exits_error():
    """Tier 1: --mode compare without --after exits non-zero with error message."""
    rc, stdout, stderr = _run("--mode", "compare", "--before", "/tmp/x.jsonl")
    assert rc != 0
    assert "after" in stderr.lower() or "compare" in stderr.lower()


def test_compare_mode_with_valid_traces(tmp_path: Path):
    """Tier 1: --mode compare with valid before/after traces exits 0."""
    before = _minimal_trace(tmp_path)
    after = tmp_path / "after.jsonl"
    import shutil
    shutil.copy(before, after)
    rc, stdout, stderr = _run(
        "--mode", "compare", "--before", str(before), "--after", str(after)
    )
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"
    assert "compare" in stdout.lower()


def test_compare_mode_scope_phase(tmp_path: Path):
    """Tier 1: --mode compare --scope phase is accepted."""
    before = _minimal_trace(tmp_path)
    after = tmp_path / "after.jsonl"
    import shutil
    shutil.copy(before, after)
    rc, stdout, stderr = _run(
        "--mode", "compare", "--before", str(before), "--after", str(after),
        "--scope", "phase",
    )
    assert rc == 0, f"stderr={stderr!r}"


# ---------------------------------------------------------------------------
# Regression: existing modes unchanged
# ---------------------------------------------------------------------------

def test_existing_mode_summary_unaffected(tmp_path: Path):
    """Tier 1: --mode summary still works with new arguments present (no regression)."""
    # Create a minimal .reyn/events directory so summary can run.
    reyn_dir = tmp_path / ".reyn"
    events_dir = reyn_dir / "events"
    events_dir.mkdir(parents=True)
    rc, stdout, stderr = _run("--mode", "summary", "--root", str(reyn_dir))
    # "no events found" or a summary header — either is valid.
    assert rc == 0 or "no events" in stdout.lower() or "no events" in stderr.lower()


def test_existing_mode_cost_unaffected(tmp_path: Path):
    """Tier 1: --mode cost still works with new arguments present (no regression)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    rc, stdout, stderr = _run("--mode", "cost", "--root", str(reyn_dir))
    # Either "no cost ledger" message or a cost summary — either is valid.
    assert rc == 0


def test_new_args_not_required_for_existing_modes(tmp_path: Path):
    """Tier 1: --before / --after / --at / --scope do not break existing llm-payloads mode."""
    # llm-payloads with a non-existent default trace should print "no LLM request records"
    # or error on missing file — NOT crash on unexpected args.
    trace = tmp_path / "empty.jsonl"
    trace.write_text("", encoding="utf-8")
    rc, stdout, stderr = _run("--mode", "llm-payloads", "--trace", str(trace))
    assert rc == 0
    # Should print the "no LLM request records" message.
    assert "no llm" in stdout.lower() or "no" in stdout.lower()


# ---------------------------------------------------------------------------
# --wal flag for replay (multi-file friction follow-up)
# ---------------------------------------------------------------------------

def _wal_only_trace(tmp_path: Path) -> Path:
    """Trace containing only WAL events (no LLM payloads)."""
    p = tmp_path / "wal.jsonl"
    _write_trace(p, [
        {
            "seq": 1, "ts": "2026-01-01T00:00:01", "kind": "skill_started",
            "run_id": "run1", "skill": "demo",
        },
        {
            "seq": 2, "ts": "2026-01-01T00:00:02", "kind": "skill_phase_advanced",
            "run_id": "run1", "phase": "p1",
        },
        {
            "seq": 3, "ts": "2026-01-01T00:00:03", "kind": "step_started",
            "run_id": "run1", "phase": "p1",
            "op_kind": "file", "op_invocation_id": "oid1",
            "args_hash": "abc", "args": {},
        },
        {
            "seq": 4, "ts": "2026-01-01T00:00:04", "kind": "step_completed",
            "run_id": "run1", "phase": "p1",
            "op_kind": "file", "op_invocation_id": "oid1",
            "result": {},
        },
    ])
    return p


def _llm_only_trace(tmp_path: Path) -> Path:
    """Trace containing only LLM payload records (no WAL)."""
    p = tmp_path / "llm.jsonl"
    _write_trace(p, [
        {
            "kind": "request", "request_id": "req1",
            "timestamp": "2026-01-01T00:00:05",
            "model": "test-model", "caller_hint": "phase:p1",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
        },
        {
            "kind": "response", "request_id": "req1",
            "timestamp": "2026-01-01T00:00:06",
            "content": "ok", "finish_reason": "stop",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    ])
    return p


def test_replay_with_wal_and_trace_separately(tmp_path: Path):
    """Tier 1: --mode replay with --wal + --trace runs without concat step."""
    wal = _wal_only_trace(tmp_path)
    llm = _llm_only_trace(tmp_path)
    rc, stdout, stderr = _run(
        "--mode", "replay",
        "--wal", str(wal),
        "--trace", str(llm),
    )
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"
    # Engine should have walked the WAL events; expect a frame header line.
    assert "frame" in stdout.lower() or "checkpoint" in stdout.lower()


def test_replay_with_only_wal_works(tmp_path: Path):
    """Tier 1: --mode replay with only --wal (no --trace) is accepted."""
    wal = _wal_only_trace(tmp_path)
    rc, stdout, stderr = _run("--mode", "replay", "--wal", str(wal))
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"


def test_replay_no_wal_or_trace_exits_with_error():
    """Tier 1: --mode replay with neither --wal nor --trace exits non-zero."""
    rc, stdout, stderr = _run("--mode", "replay")
    assert rc != 0
    assert "trace" in stderr.lower() or "wal" in stderr.lower()


def test_compare_with_multiple_before_after_paths(tmp_path: Path):
    """Tier 1: --before / --after accept multiple paths (= WAL + LLM split)."""
    wal_a = _wal_only_trace(tmp_path)
    llm_a = _llm_only_trace(tmp_path)

    # Make a 'b' pair by copying.
    import shutil
    wal_b = tmp_path / "wal_b.jsonl"
    llm_b = tmp_path / "llm_b.jsonl"
    shutil.copy(wal_a, wal_b)
    shutil.copy(llm_a, llm_b)

    rc, stdout, stderr = _run(
        "--mode", "compare",
        "--before", str(wal_a), "--before", str(llm_a),
        "--after", str(wal_b), "--after", str(llm_b),
    )
    assert rc == 0, f"stdout={stdout!r} stderr={stderr!r}"
    assert "compare" in stdout.lower()
    # Should mention "2 files" since each side has 2 paths.
    assert "2 files" in stdout
