"""Tier 2: ops_report stdlib skill (FP-0009 Component D).

Tests for aggregate.py + aggregate_pure.py functions:
  aggregate_from_raw_events — walk .jsonl files and compute stats
  aggregate_from_recall_chunks — aggregate pre-fetched recall chunks

All tests use real filesystem I/O (tmp_path) and real Python functions.
No mocks, no AsyncMock, no patch decorators — per CLAUDE.md testing policy.

FP-0042 Phase 2.6 (2026-05-23): ``aggregate.py`` migrated from mode:
unsafe to mode: safe; file I/O now goes through ``reyn.api.safe.file``.
The autouse ``_safe_file_context`` fixture below grants reads under
``tmp_path`` so the safe-mode helpers can run inside the test process
(= mirrors how the production preprocessor_executor wires the
safe-mode subprocess).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reyn.api.safe import file as sf

# Module under test
from reyn.stdlib.skills.ops_report.aggregate import (
    aggregate_from_raw_events,
    collect_aggregate_fallback,
)
from reyn.stdlib.skills.ops_report.aggregate_pure import (
    aggregate_from_recall_chunks,
    dispatch_aggregate,
)


def collect_aggregate(artifact: dict) -> dict:
    """Test helper: dispatches to dispatch_aggregate then collect_aggregate_fallback.

    Was the back-compat wrapper in ``aggregate.py`` before FP-0042 Phase 2.6;
    moved here because the active preprocessor chain calls the two steps
    directly via skill.md, and the only consumer was this test module.

    Hosting the composition in the test module also avoids the cross-module
    ``reyn.stdlib.*`` import the safe-mode AST validator rejects.
    """
    import copy

    dispatched = dispatch_aggregate(artifact)
    patched = copy.deepcopy(artifact)
    data = patched.setdefault("data", {})
    data["aggregate"] = dispatched
    return collect_aggregate_fallback(patched)


@pytest.fixture(autouse=True)
def _safe_file_context(tmp_path: Path):
    """Grant reyn.api.safe.file read access over tmp_path for each test.

    Mirrors the production wiring (= CWD as default read zone). Tests
    in this module operate exclusively under tmp_path, so a wide grant
    there matches the per-test sandbox model.
    """
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    sf._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path)],
    )
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False

# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    """Format datetime as ISO-8601 with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _days_ago(n: float) -> datetime:
    return _now() - timedelta(days=n)


def _write_events(
    path: Path,
    *,
    skill: str,
    run_id: str,
    status: str = "success",
    started_offset_days: float = 1.0,
    errors: list[str] | None = None,
) -> None:
    """Write a minimal complete run as two events to a .jsonl file."""
    started_at = _days_ago(started_offset_days)
    completed_at = started_at + timedelta(seconds=10)
    status_field = "success" if status == "success" else "failed"

    events = [
        {
            "type": "run_skill_started",
            "timestamp": _iso(started_at),
            "data": {
                "run_id": run_id,
                "skill": skill,
                "started_at": _iso(started_at),
            },
        },
    ]

    # Error events if provided
    if errors:
        for msg in errors:
            events.append({
                "type": "error",
                "timestamp": _iso(started_at + timedelta(seconds=5)),
                "data": {
                    "run_id": run_id,
                    "msg": msg,
                },
            })

    if status == "success":
        completion_type = "run_skill_completed"
    else:
        completion_type = "run_skill_failed"

    events.append({
        "type": completion_type,
        "timestamp": _iso(completed_at),
        "data": {
            "run_id": run_id,
            "skill": skill,
            "status": status_field,
            "completed_at": _iso(completed_at),
        },
    })

    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# ── Test 1: basic aggregation ──────────────────────────────────────────────────


def test_aggregate_from_raw_events_basic(tmp_path: Path) -> None:
    """Tier 2: 5 runs (3 success, 2 fail) → correct total_runs/success_rate/by_skill."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    _write_events(log, skill="my_skill", run_id="r1", status="success")
    _write_events(log, skill="my_skill", run_id="r2", status="success")
    _write_events(log, skill="my_skill", run_id="r3", status="success")
    _write_events(log, skill="my_skill", run_id="r4", status="failed")
    _write_events(log, skill="my_skill", run_id="r5", status="failed")

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=None)

    assert result["total_runs"] == 5
    assert result["success_count"] == 3
    assert result["failure_count"] == 2
    assert abs(result["success_rate"] - 0.6) < 1e-9

    assert "my_skill" in result["by_skill"]
    sk = result["by_skill"]["my_skill"]
    assert sk["count"] == 5
    assert sk["success"] == 3
    assert sk["failure"] == 2


# ── Test 2: period filter ──────────────────────────────────────────────────────


def test_aggregate_from_raw_events_period_filter(tmp_path: Path) -> None:
    """Tier 2: runs across 14 days; period_days=7 only counts last week."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    # 3 runs within last 7 days
    _write_events(log, skill="sk", run_id="recent1", status="success", started_offset_days=1)
    _write_events(log, skill="sk", run_id="recent2", status="success", started_offset_days=3)
    _write_events(log, skill="sk", run_id="recent3", status="failed", started_offset_days=6)

    # 2 runs older than 7 days
    _write_events(log, skill="sk", run_id="old1", status="success", started_offset_days=8)
    _write_events(log, skill="sk", run_id="old2", status="success", started_offset_days=13)

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=None)

    assert result["total_runs"] == 3
    assert result["success_count"] == 2
    assert result["failure_count"] == 1


# ── Test 3: skills filter ──────────────────────────────────────────────────────


def test_aggregate_from_raw_events_skills_filter(tmp_path: Path) -> None:
    """Tier 2: 2 skills; filter to one; assert other excluded."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    _write_events(log, skill="skill_a", run_id="a1", status="success")
    _write_events(log, skill="skill_a", run_id="a2", status="success")
    _write_events(log, skill="skill_b", run_id="b1", status="failed")
    _write_events(log, skill="skill_b", run_id="b2", status="success")

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=["skill_a"])

    assert result["total_runs"] == 2
    assert "skill_a" in result["by_skill"]
    assert "skill_b" not in result["by_skill"]


# ── Test 4: empty / nonexistent root ──────────────────────────────────────────


def test_aggregate_from_raw_events_empty_root(tmp_path: Path) -> None:
    """Tier 2: nonexistent events dir → valid empty aggregate (total_runs=0)."""
    nonexistent = tmp_path / "no_events_here"

    result = aggregate_from_raw_events(str(nonexistent), period_days=7, skills=None)

    assert result["total_runs"] == 0
    assert result["success_count"] == 0
    assert result["failure_count"] == 0
    assert result["success_rate"] is None
    assert result["by_skill"] == {}
    assert result["top_failing_skills"] == []
    assert result["errors_sample"] == []


# ── Test 5: aggregate_from_recall_chunks basic ────────────────────────────────


def test_aggregate_from_recall_chunks_basic() -> None:
    """Tier 2: synthetic recall chunks → correct output shape."""
    chunks = [
        {
            "content": "skill: my_skill\nstatus: success",
            "metadata": {
                "extra": {
                    "skill": "my_skill",
                    "status": "success",
                    "duration_seconds": 10,
                    "errors": [],
                }
            },
        },
        {
            "content": "skill: my_skill\nstatus: failed",
            "metadata": {
                "extra": {
                    "skill": "my_skill",
                    "status": "failed",
                    "duration_seconds": 5,
                    "errors": ["something went wrong"],
                }
            },
        },
        {
            "content": "skill: other_skill\nstatus: success",
            "metadata": {
                "extra": {
                    "skill": "other_skill",
                    "status": "success",
                    "duration_seconds": 20,
                    "errors": [],
                }
            },
        },
    ]

    result = aggregate_from_recall_chunks(chunks)

    assert result["total_runs"] == 3
    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert abs(result["success_rate"] - 2 / 3) < 1e-9

    assert "my_skill" in result["by_skill"]
    assert "other_skill" in result["by_skill"]

    my_sk = result["by_skill"]["my_skill"]
    assert my_sk["count"] == 2
    assert my_sk["success"] == 1
    assert my_sk["failure"] == 1
    assert my_sk["avg_duration_seconds"] == pytest.approx(7.5)

    # period_days is None for recall-based path
    assert result["period_days"] is None


# ── Test 6: in-flight runs excluded ───────────────────────────────────────────


def test_aggregate_handles_inflight_runs(tmp_path: Path) -> None:
    """Tier 2: incomplete runs (no run_skill_completed) excluded from totals."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    # One complete run
    _write_events(log, skill="sk", run_id="complete", status="success")

    # One incomplete run — only started event, no completion
    started_at = _days_ago(1)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "run_skill_started",
            "timestamp": _iso(started_at),
            "data": {
                "run_id": "inflight",
                "skill": "sk",
                "started_at": _iso(started_at),
            },
        }) + "\n")

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=None)

    # Only the complete run should count
    assert result["total_runs"] == 1
    assert result["success_count"] == 1


# ── Test 7: top_failing_skills sorted descending ──────────────────────────────


def test_top_failing_skills_sorted_desc(tmp_path: Path) -> None:
    """Tier 2: top_failing_skills sorted by failure_count descending."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    # skill_a: 3 failures
    for i in range(3):
        _write_events(log, skill="skill_a", run_id=f"a{i}", status="failed")

    # skill_b: 1 failure
    _write_events(log, skill="skill_b", run_id="b0", status="failed")

    # skill_c: 5 failures
    for i in range(5):
        _write_events(log, skill="skill_c", run_id=f"c{i}", status="failed")

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=None)

    top = result["top_failing_skills"]
    (first, second, third) = top

    # Verify descending order by failure_count
    failure_counts = [entry["failure_count"] for entry in top]
    assert failure_counts == sorted(failure_counts, reverse=True), (
        f"top_failing_skills not sorted descending: {failure_counts}"
    )

    # skill_c should be first (5 failures)
    assert first["skill"] == "skill_c"
    assert first["failure_count"] == 5


# ── Test 8: error samples collected ───────────────────────────────────────────


def test_aggregate_collects_error_samples(tmp_path: Path) -> None:
    """Tier 2: failed run with error events → error sample appears in output."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "runs.jsonl"

    error_msgs = ["FileNotFoundError: .reyn/workspace/foo", "PermissionError: write denied"]
    _write_events(
        log,
        skill="my_skill",
        run_id="fail_run",
        status="failed",
        errors=error_msgs,
    )

    result = aggregate_from_raw_events(str(events_dir), period_days=7, skills=None)

    assert result["total_runs"] == 1
    assert result["failure_count"] == 1
    assert len(result["errors_sample"]) > 0, "Expected at least one error in errors_sample"
    # At least one error message should appear in the sample
    all_samples = " ".join(result["errors_sample"])
    assert "FileNotFoundError" in all_samples or "PermissionError" in all_samples, (
        f"Expected error messages in errors_sample; got: {result['errors_sample']}"
    )


# ── collect_aggregate (= preprocessor entry point, R-1 fix) ──────────────────


def test_collect_aggregate_prefers_recall_when_chunks_present() -> None:
    """Tier 2: collect_aggregate uses recall chunks when non-empty (preferred path)."""
    # Chunk shape matches aggregate_from_recall_chunks expectation: metadata.extra.*
    chunks = [
        {
            "content": "skill: my_skill\nstatus: success",
            "metadata": {
                "extra": {"skill": "my_skill", "status": "success", "duration_seconds": 12, "errors": []}
            },
        },
        {
            "content": "skill: my_skill\nstatus: failed",
            "metadata": {
                "extra": {"skill": "my_skill", "status": "failed", "duration_seconds": 8, "errors": ["err"]}
            },
        },
    ]
    artifact = {
        "data": {
            "recall_result": {"chunks": chunks, "mode": "semantic"},
            "period_days": 7,
            "skills": None,
        }
    }
    result = collect_aggregate(artifact)
    assert result["total_runs"] == 2
    # Recall-path observable: by_skill populated from chunks
    assert "my_skill" in result["by_skill"]


def test_collect_aggregate_falls_back_to_raw_events_when_recall_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: collect_aggregate walks .reyn/events when recall returned no chunks."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "2026-05-15.jsonl"
    _write_events(log, skill="my_skill", run_id="r1", status="success")

    # collect_aggregate uses the relative path ".reyn/events" → chdir into tmp_path.
    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "recall_result": {"chunks": [], "mode": "fallback"},
            "period_days": 7,
            "skills": None,
        }
    }
    result = collect_aggregate(artifact)
    assert result["total_runs"] == 1


def test_collect_aggregate_handles_missing_recall_result(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: collect_aggregate handles `recall_result=None` (= on_error: skip path)."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "2026-05-15.jsonl"
    _write_events(log, skill="my_skill", run_id="r1", status="success")
    monkeypatch.chdir(tmp_path)

    artifact = {"data": {"recall_result": None, "period_days": 7, "skills": None}}
    result = collect_aggregate(artifact)
    assert result["total_runs"] == 1


def test_collect_aggregate_default_period_days_when_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: collect_aggregate defaults period_days=7 if input artifact omits it.

    Isolate via monkeypatch.chdir(tmp_path) — without it the relative
    `.reyn/events` resolves to the project dir and picks up real events.
    """
    monkeypatch.chdir(tmp_path)
    artifact = {"data": {"recall_result": {"chunks": []}}}
    # No events dir under tmp_path → empty result, but no crash.
    result = collect_aggregate(artifact)
    assert result["total_runs"] == 0
    # period reported in human-readable form should reflect default 7
    assert "7" in result.get("period", "") or result.get("period_days") == 7


# ── BUG-3 regression: skill.md compile + phase file existence ────────────────


def test_ops_report_skill_md_compiles() -> None:
    """Tier 2: ops_report skill.md compiles without 'Phase not found' error (BUG-3 regression)."""
    from pathlib import Path

    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "ops_report" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent  # src/reyn/stdlib/

    assert skill_md.exists(), f"skill.md not found at {skill_md}"
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.name == "ops_report"
    assert skill.entry_phase == "collect"
    assert "collect" in {p for p in skill.phases}
    assert "summarize" in {p for p in skill.phases}


def test_ops_report_skill_has_collect_phase_file() -> None:
    """Tier 2: ops_report/phases/collect.md exists on disk (BUG-3 regression)."""
    from pathlib import Path

    collect_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "ops_report" / "phases" / "collect.md"
    )
    assert collect_md.exists(), f"Expected {collect_md} to exist"


# ── R-PURE-MODE-REDEFINE wave 2: safe-mode import contract ───────────────────


def test_aggregate_pure_imports_only_safe_modules() -> None:
    """Tier 2: aggregate_pure.py imports only PURE_STDLIB_ALLOWLIST modules (R-PURE-MODE).

    AST-walks aggregate_pure.py and asserts every imported top-level module is
    in PURE_STDLIB_ALLOWLIST (or ``__future__``). This pins the R-PURE-MODE-REDEFINE
    wave 2 guarantee: the mode: safe declaration in skill.md is honest.
    """
    import ast
    from pathlib import Path

    from reyn.core.kernel._python_allowlist import PURE_STDLIB_ALLOWLIST

    src = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "ops_report" / "aggregate_pure.py"
    ).read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    forbidden = imported - PURE_STDLIB_ALLOWLIST - {"__future__"}
    assert not forbidden, f"aggregate_pure.py imports non-safe modules: {forbidden}"


# ── R-PURE-MODE-REDEFINE wave 3a: dispatch_aggregate / collect_aggregate_fallback ─


def test_dispatch_aggregate_with_recall_chunks_returns_recall_path() -> None:
    """Tier 2: dispatch_aggregate uses recall chunks when non-empty → _path=recall, stats computed."""
    chunks = [
        {
            "content": "skill: sk\nstatus: success",
            "metadata": {
                "extra": {"skill": "sk", "status": "success", "duration_seconds": 5, "errors": []}
            },
        },
        {
            "content": "skill: sk\nstatus: failed",
            "metadata": {
                "extra": {"skill": "sk", "status": "failed", "duration_seconds": 3, "errors": ["oops"]}
            },
        },
    ]
    artifact = {
        "data": {
            "recall_result": {"chunks": chunks, "mode": "semantic"},
            "period_days": 7,
            "skills": None,
        }
    }
    result = dispatch_aggregate(artifact)

    assert result["_path"] == "recall"
    assert result["total_runs"] == 2
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    assert "sk" in result["by_skill"]


def test_dispatch_aggregate_without_chunks_returns_fallback_sentinel() -> None:
    """Tier 2: dispatch_aggregate emits needs_fallback sentinel when recall has no chunks."""
    artifact = {
        "data": {
            "recall_result": {"chunks": [], "mode": "fallback"},
            "period_days": 14,
            "skills": ["my_skill"],
        }
    }
    result = dispatch_aggregate(artifact)

    assert result["_path"] == "needs_fallback"
    assert result["period_days"] == 14
    assert result["skills"] == ["my_skill"]
    # No stats fields (full aggregate not yet computed)
    assert "total_runs" not in result


def test_collect_aggregate_fallback_no_ops_when_upstream_recalled() -> None:
    """Tier 2: collect_aggregate_fallback strips _path sentinel and returns recall stats unchanged."""
    upstream_stats = {
        "_path": "recall",
        "total_runs": 3,
        "success_count": 2,
        "failure_count": 1,
        "success_rate": 2 / 3,
        "period_days": None,
        "by_skill": {"sk": {"count": 3, "success": 2, "failure": 1, "avg_duration_seconds": 7.0}},
        "top_failing_skills": [{"skill": "sk", "failure_count": 1, "total_count": 3}],
        "errors_sample": ["err1"],
    }
    artifact = {
        "data": {
            "aggregate": upstream_stats,
        }
    }
    result = collect_aggregate_fallback(artifact)

    # Sentinel stripped
    assert "_path" not in result
    # Stats passed through verbatim
    assert result["total_runs"] == 3
    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert result["by_skill"]["sk"]["count"] == 3
    assert result["errors_sample"] == ["err1"]


def test_collect_aggregate_fallback_walks_raw_events_when_needed(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: collect_aggregate_fallback walks .reyn/events when aggregate._path=needs_fallback."""
    events_dir = tmp_path / ".reyn" / "events"
    events_dir.mkdir(parents=True)
    log = events_dir / "runs.jsonl"
    _write_events(log, skill="raw_skill", run_id="r1", status="success")
    _write_events(log, skill="raw_skill", run_id="r2", status="failed")

    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "aggregate": {
                "_path": "needs_fallback",
                "period_days": 7,
                "skills": None,
            }
        }
    }
    result = collect_aggregate_fallback(artifact)

    assert "_path" not in result
    assert result["total_runs"] == 2
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    assert "raw_skill" in result["by_skill"]
