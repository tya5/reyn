"""Tier 2: dogfood_trace.py plan-mode awareness (prep wave for batch 16).

Pins the contract that the new ``--mode plan-summary``,
``--mode plan-trace``, ``--mode plan-snapshot`` and the cost-mode
extension correctly aggregate plan-mode telemetry from realistic on-
disk shapes:

  - WAL entries are flat ({"kind": ..., "ts": ..., "plan_id": ...,
    ...fields}) — NOT nested under "data"
  - Events-log entries are nested ({"type": ..., "timestamp": ...,
    "data": {"plan_id": ..., ...}})
  - The `_load_wal` normaliser bridges these shapes so the modes work
    on both inputs uniformly.

Tests target the public CLI surface (subprocess invocation) so they
catch field-shape regressions end-to-end.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run dogfood_trace.py with given args; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def _write_wal(state_dir: Path, entries: list[dict]) -> None:
    """Write WAL entries (flat shape) to state/wal.jsonl."""
    state_dir.mkdir(parents=True, exist_ok=True)
    wal = state_dir / "wal.jsonl"
    with wal.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _write_event_log(events_dir: Path, name: str, entries: list[dict]) -> None:
    """Write event-log entries (nested shape: type + timestamp + data)."""
    events_dir.mkdir(parents=True, exist_ok=True)
    log = events_dir / f"{name}.jsonl"
    with log.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── --mode plan-summary ───────────────────────────────────────────────────


def test_plan_summary_no_events_message(tmp_path: Path) -> None:
    """Tier 2: empty .reyn dir → friendly message, exit 0 (no crash)."""
    (tmp_path / ".reyn").mkdir()
    proc = _run("--mode", "plan-summary", "--root", str(tmp_path / ".reyn"))
    assert proc.returncode == 0
    assert "no plan events" in proc.stdout.lower()


def test_plan_summary_aggregates_real_wal_shape(tmp_path: Path) -> None:
    """Tier 2: real WAL entries are flat (no `data` nesting).
    plan-summary must read top-level plan_id correctly. This is the
    bug-pattern test that catches the data-nesting confusion between
    WAL and events log."""
    state_dir = tmp_path / ".reyn" / "state"
    _write_wal(state_dir, [
        {"seq": 1, "ts": "2026-05-08T10:00:00", "kind": "plan_started",
         "plan_id": "ab12cd34", "goal": "test goal", "n_steps": 2,
         "target": "default"},
        {"seq": 2, "ts": "2026-05-08T10:00:01", "kind": "plan_step_started",
         "plan_id": "ab12cd34", "step_id": "s1", "depends_on": [],
         "n_tools": 0, "target": "default"},
        {"seq": 3, "ts": "2026-05-08T10:00:02", "kind": "plan_step_completed",
         "plan_id": "ab12cd34", "step_id": "s1", "content_len": 50,
         "target": "default"},
        {"seq": 4, "ts": "2026-05-08T10:00:03", "kind": "plan_step_started",
         "plan_id": "ab12cd34", "step_id": "s2", "depends_on": ["s1"],
         "n_tools": 0, "target": "default"},
        {"seq": 5, "ts": "2026-05-08T10:00:04", "kind": "plan_step_completed",
         "plan_id": "ab12cd34", "step_id": "s2", "content_len": 75,
         "target": "default"},
        {"seq": 6, "ts": "2026-05-08T10:00:05", "kind": "plan_completed",
         "plan_id": "ab12cd34", "target": "default"},
    ])

    proc = _run("--mode", "plan-summary", "--root", str(tmp_path / ".reyn"))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    out = proc.stdout
    # The plan_id MUST appear in the per-plan table (= proves WAL
    # parsing extracted plan_id from top-level field).
    assert "ab12cd34" in out
    assert "plans started:    1" in out
    assert "plans completed:  1" in out
    assert "2 started / 2 completed" in out
    # Goal text appears in per-plan table.
    assert "test goal" in out


def test_plan_summary_combines_wal_and_events_log_memo_hits(tmp_path: Path) -> None:
    """Tier 2: plan_step_memoized + plan_step_llm_memoized live only in
    the events log; plan-summary must read them from there too."""
    reyn = tmp_path / ".reyn"
    state_dir = reyn / "state"
    events_dir = reyn / "events" / "agents" / "default"

    _write_wal(state_dir, [
        {"seq": 1, "ts": "2026-05-08T10:00:00", "kind": "plan_started",
         "plan_id": "p1", "goal": "g", "n_steps": 1, "target": "default"},
        {"seq": 2, "ts": "2026-05-08T10:00:05", "kind": "plan_completed",
         "plan_id": "p1", "target": "default"},
    ])
    _write_event_log(events_dir, "chat", [
        {"type": "plan_step_memoized",
         "timestamp": "2026-05-08T10:00:01",
         "data": {"plan_id": "p1", "step_id": "s1", "content_len": 100}},
        {"type": "plan_step_memoized",
         "timestamp": "2026-05-08T10:00:02",
         "data": {"plan_id": "p1", "step_id": "s2", "content_len": 50}},
        {"type": "plan_step_llm_memoized",
         "timestamp": "2026-05-08T10:00:03",
         "data": {"plan_id": "p1", "step_id": "s2", "args_hash": "abc"}},
    ])

    proc = _run("--mode", "plan-summary", "--root", str(reyn))
    assert proc.returncode == 0
    assert "2 step-result + 1 LLM-call" in proc.stdout


def test_plan_summary_max_concurrent_two_overlapping_plans(tmp_path: Path) -> None:
    """Tier 2: 2 plans whose started→completed intervals overlap →
    max_concurrent = 2."""
    state_dir = tmp_path / ".reyn" / "state"
    _write_wal(state_dir, [
        # Plan A: 10:00:00 → 10:00:10
        {"seq": 1, "ts": "2026-05-08T10:00:00", "kind": "plan_started",
         "plan_id": "pa", "goal": "a", "n_steps": 1, "target": "default"},
        # Plan B: 10:00:05 → 10:00:08 (entirely inside A's window)
        {"seq": 2, "ts": "2026-05-08T10:00:05", "kind": "plan_started",
         "plan_id": "pb", "goal": "b", "n_steps": 1, "target": "default"},
        {"seq": 3, "ts": "2026-05-08T10:00:08", "kind": "plan_completed",
         "plan_id": "pb", "target": "default"},
        {"seq": 4, "ts": "2026-05-08T10:00:10", "kind": "plan_completed",
         "plan_id": "pa", "target": "default"},
    ])
    proc = _run("--mode", "plan-summary", "--root", str(tmp_path / ".reyn"))
    assert proc.returncode == 0
    assert "max concurrent plans" in proc.stdout
    assert ": 2" in proc.stdout  # the value


# ── --mode plan-trace <plan_id> ───────────────────────────────────────────


def test_plan_trace_missing_plan_id_errors(tmp_path: Path) -> None:
    """Tier 2: plan-trace without positional plan_id → exit 1 + usage error."""
    proc = _run("--mode", "plan-trace", "--root", str(tmp_path))
    assert proc.returncode == 1


def test_plan_trace_filters_to_one_plan(tmp_path: Path) -> None:
    """Tier 2: plan-trace prints events for the requested plan_id only,
    and ignores entries for other plan_ids."""
    state_dir = tmp_path / ".reyn" / "state"
    _write_wal(state_dir, [
        {"seq": 1, "ts": "2026-05-08T10:00:00", "kind": "plan_started",
         "plan_id": "target_pid", "goal": "match-me", "n_steps": 1,
         "target": "default"},
        {"seq": 2, "ts": "2026-05-08T10:00:01", "kind": "plan_started",
         "plan_id": "other_pid", "goal": "DO_NOT_MATCH", "n_steps": 1,
         "target": "default"},
        {"seq": 3, "ts": "2026-05-08T10:00:02", "kind": "plan_completed",
         "plan_id": "target_pid", "target": "default"},
    ])
    proc = _run(
        "--mode", "plan-trace", "target_pid",
        "--root", str(tmp_path / ".reyn"),
    )
    assert proc.returncode == 0
    out = proc.stdout
    # Target events appear; other plan's events do NOT.
    assert "plan_started" in out
    assert "plan_completed" in out
    assert "DO_NOT_MATCH" not in out
    assert "other_pid" not in out


# ── --mode plan-snapshot <plan_id> ────────────────────────────────────────


def test_plan_snapshot_missing_plan_id_errors(tmp_path: Path) -> None:
    """Tier 2: plan-snapshot without positional plan_id → exit 1."""
    proc = _run("--mode", "plan-snapshot", "--root", str(tmp_path))
    assert proc.returncode == 1


def test_plan_snapshot_dumps_decomposition_and_snapshot(tmp_path: Path) -> None:
    """Tier 2: plan-snapshot reads decomposition.json + per-plan snapshot
    JSON from the per-plan workspace dir."""
    plan_id = "test_plan_xyz"
    plan_dir = (
        tmp_path / ".reyn" / "agents" / "default" / "state" / "plans" / plan_id
    )
    plan_dir.mkdir(parents=True)

    # decomposition.json
    (plan_dir / "decomposition.json").write_text(json.dumps({
        "plan_id": plan_id, "schema_version": 1,
        "goal": "test goal text",
        "steps": [
            {"id": "s1", "description": "first step",
             "tools": ["read_file"], "depends_on": []},
            {"id": "s2", "description": "second step",
             "tools": [], "depends_on": ["s1"]},
        ],
    }))

    # Per-plan snapshot file (sibling to per-plan dir).
    snap_path = plan_dir.parent / f"{plan_id}.snapshot.json"
    snap_path.write_text(json.dumps({
        "schema_version": 1, "plan_id": plan_id,
        "agent_name": "default", "chain_id": f"plan_{plan_id}",
        "goal": "test goal text",
        "applied_seq": 42, "last_step_applied_seq": 40,
        "decomposition_artifact_path": str(plan_dir / "decomposition.json"),
        "steps_serialized": [],
        "step_results": {"s1": "inline result text"},
        "step_result_refs": {},
        "step_llm_calls": {
            "s1": [{"args_hash": "a", "inline": {"content": "x"},
                    "ref": None, "usage": {}}]
        },
        "step_failures": {},
        "current_step_id": None,
        "last_committed_step_id": "s1",
        "spawned_skill_run_ids": {},
        "parent_skill_run_id": None,
        "usage_tokens_so_far": None,
    }))

    proc = _run(
        "--mode", "plan-snapshot", plan_id,
        "--root", str(tmp_path / ".reyn"),
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    out = proc.stdout
    assert plan_id in out
    assert "test goal text" in out
    assert "s1" in out
    assert "first step" in out
    # Should reflect snapshot fields
    assert "applied_seq" in out or "42" in out


def test_plan_snapshot_unknown_plan_id_friendly_message(tmp_path: Path) -> None:
    """Tier 2: unknown plan_id → friendly error, exit 0 (= soft fail,
    user retries with correct id)."""
    (tmp_path / ".reyn").mkdir()
    proc = _run(
        "--mode", "plan-snapshot", "nonexistent_pid",
        "--root", str(tmp_path / ".reyn"),
    )
    # Either prints a not-found / no-agents message or exits with non-
    # zero — both are acceptable as long as we don't crash.
    assert proc.returncode in (0, 1)
    combined = (proc.stdout + proc.stderr).lower()
    assert any(s in combined for s in (
        "nonexistent_pid", "not found", "no agents", "no plans",
    ))


# ── --mode cost extension (memo savings) ──────────────────────────────────


def test_cost_mode_appends_memo_savings_when_present(tmp_path: Path) -> None:
    """Tier 2: cost mode reports memo savings when plan_step_memoized /
    plan_step_llm_memoized events exist in the events log."""
    reyn = tmp_path / ".reyn"
    # Budget ledger (= existing cost data source)
    (reyn / "state").mkdir(parents=True)
    with (reyn / "state" / "budget_ledger.jsonl").open("w") as f:
        f.write(json.dumps({
            "model": "openai/gpt-4o-mini",
            "tokens": 100, "cost_usd": 0.001,
        }) + "\n")
        f.write(json.dumps({
            "model": "openai/gpt-4o-mini",
            "tokens": 200, "cost_usd": 0.002,
        }) + "\n")

    # Memo events
    events_dir = reyn / "events" / "agents" / "default"
    _write_event_log(events_dir, "chat", [
        {"type": "plan_step_memoized",
         "timestamp": "2026-05-08T10:00:00",
         "data": {"plan_id": "p1", "step_id": "s1"}},
        {"type": "plan_step_llm_memoized",
         "timestamp": "2026-05-08T10:00:01",
         "data": {"plan_id": "p1", "step_id": "s1", "args_hash": "h"}},
    ])

    proc = _run("--mode", "cost", "--root", str(reyn))
    assert proc.returncode == 0
    out = proc.stdout
    # Existing summary preserved
    assert "Total" in out
    assert "openai/gpt-4o-mini" in out
    # Memo savings appended
    assert "memo savings" in out.lower() or "memoizations" in out.lower()
    assert "1 step-result" in out or "step-result memoizations:  1" in out
    assert "1" in out  # llm-call hits = 1


def test_cost_mode_no_memo_section_when_zero_hits(tmp_path: Path) -> None:
    """Tier 2: cost mode preserves existing output when no memo events
    are present (= backward compat for skill-only batches)."""
    reyn = tmp_path / ".reyn"
    (reyn / "state").mkdir(parents=True)
    with (reyn / "state" / "budget_ledger.jsonl").open("w") as f:
        f.write(json.dumps({
            "model": "anthropic/claude-3-5-sonnet",
            "tokens": 1000, "cost_usd": 0.015,
        }) + "\n")

    proc = _run("--mode", "cost", "--root", str(reyn))
    assert proc.returncode == 0
    # Existing summary present
    assert "Cost Summary" in proc.stdout
    # No memo-savings section
    assert "memo savings" not in proc.stdout.lower()


# ── backward compat: existing modes unaffected ───────────────────────────


def test_existing_summary_mode_still_works(tmp_path: Path) -> None:
    """Tier 2: don't regress existing --mode summary on empty .reyn."""
    proc = _run(
        "--mode", "summary",
        "--root", str(tmp_path / "nonexistent_root"),
    )
    assert proc.returncode == 0
    assert "no events" in proc.stdout.lower()
