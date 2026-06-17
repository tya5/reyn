"""Tier 2: skill_improver collect_traces phase (FP-0006 Component C).

Tests for:
  - DSL compile-time assertions (skill graph, phase file existence)
  - trace_collector + trace_collector_pure functional behavior
    (recall path, raw-events fallback, empty data, skill-name filtering,
     lookback cap, error aggregation)
  - R-PURE-MODE wave 4: dispatch_traces / collect_traces_fallback split
    (dispatcher recall path, dispatcher fallback sentinel,
     fallback no-op when upstream recalled, fallback walks raw events)

No mocks, no AsyncMock, no patch decorators — per CLAUDE.md testing policy.
Real filesystem I/O via tmp_path; real Python function calls.

FP-0042 Phase 2.7 (2026-05-23): ``trace_collector.py`` migrated from
mode: unsafe to mode: safe; file I/O now goes through ``reyn.safe.file``.
The autouse ``_safe_file_context`` fixture grants reads under
``tmp_path`` so the safe-mode helpers can run inside the test process.

The legacy ``collect_traces`` back-compat wrapper was removed from
production — its composition is hosted here as a local test helper
since cross-module ``reyn.stdlib.*`` imports are rejected by the
safe-mode AST validator.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reyn.safe import file as sf
from reyn.stdlib.skills.skill_improver.trace_collector import collect_traces_fallback
from reyn.stdlib.skills.skill_improver.trace_collector_pure import dispatch_traces


def collect_traces(artifact: dict) -> dict:
    """Test helper: dispatches to dispatch_traces then collect_traces_fallback.

    Was the back-compat wrapper in ``trace_collector.py`` before FP-0042
    Phase 2.7; relocated here because the active preprocessor chain in
    ``phases/collect_traces.md`` calls the two underlying steps directly
    via skill.md, and the only consumer was this test module.
    """
    dispatched = dispatch_traces(artifact)
    patched = copy.deepcopy(artifact)
    data = patched.setdefault("data", {})
    data["traces_summary"] = dispatched
    return collect_traces_fallback(patched)


@pytest.fixture(autouse=True)
def _safe_file_context(tmp_path: Path):
    """Grant reyn.safe.file read access over tmp_path for each test."""
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
    skill_version_hash: str | None = None,
) -> None:
    """Write a minimal complete run as two (or more) events to a .jsonl file."""
    started_at = _days_ago(started_offset_days)
    completed_at = started_at + timedelta(seconds=10)

    started_data: dict = {
        "run_id": run_id,
        "skill": skill,
        "started_at": _iso(started_at),
    }
    if skill_version_hash:
        started_data["skill_version_hash"] = skill_version_hash

    events: list[dict] = [
        {
            "type": "run_skill_started",
            "timestamp": _iso(started_at),
            "data": started_data,
        },
    ]

    # Error events
    if errors:
        for msg in errors:
            events.append({
                "type": "error",
                "timestamp": _iso(started_at + timedelta(seconds=5)),
                "data": {
                    "run_id": run_id,
                    "msg": msg,
                    "message": msg,
                },
            })

    completion_type = "run_skill_completed" if status == "success" else "run_skill_failed"
    status_field = "success" if status == "success" else "failed"

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


def _make_recall_chunk(
    skill: str,
    status: str = "success",
    duration: float | None = 10.0,
    errors: list[str] | None = None,
    skill_version_hash: str | None = None,
) -> dict:
    """Build a synthetic recall chunk matching the FP-0009 chunk shape."""
    return {
        "content": f"skill: {skill}\nstatus: {status}",
        "metadata": {
            "extra": {
                "skill": skill,
                "status": status,
                "duration_seconds": duration,
                "errors": errors or [],
                "skill_version_hash": skill_version_hash or "unknown",
                "phases": [],
            }
        },
    }


# ── Test 1: skill.md compiles and graph contains collect_traces ───────────────


def test_collect_traces_skill_md_compiles() -> None:
    """Tier 2: skill_improver skill.md compiles; collect_traces in graph → copy_to_work."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "skill_improver" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent  # src/reyn/stdlib/

    assert skill_md.exists(), f"skill.md not found at {skill_md}"
    skill = load_dsl_skill(skill_md, skill_root=skill_root)

    assert skill.name == "skill_improver"
    transitions = skill.graph.transitions
    # collect_traces must appear in the graph
    assert "collect_traces" in transitions, (
        f"'collect_traces' missing from skill graph: {list(transitions.keys())}"
    )
    # collect_traces must transition to copy_to_work
    ct_transitions = transitions["collect_traces"]
    assert "copy_to_work" in ct_transitions, (
        f"collect_traces → copy_to_work missing; got: {ct_transitions}"
    )
    # prepare must allow both collect_traces and copy_to_work
    prepare_transitions = transitions.get("prepare", [])
    assert "collect_traces" in prepare_transitions, (
        f"prepare → collect_traces missing; got: {prepare_transitions}"
    )
    assert "copy_to_work" in prepare_transitions, (
        f"prepare → copy_to_work missing; got: {prepare_transitions}"
    )


# ── Test 2: collect_traces phase file exists on disk ─────────────────────────


def test_collect_traces_phase_file_exists() -> None:
    """Tier 2: phases/collect_traces.md exists on disk."""
    phase_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "skill_improver"
        / "phases" / "collect_traces.md"
    )
    assert phase_md.exists(), f"Expected phase file at {phase_md}"


# ── Test 3: recall path with matching chunks ──────────────────────────────────


def test_trace_collector_recall_path_with_chunks() -> None:
    """Tier 2: non-empty recall chunks → data_source=='recall', runs counted, markdown present."""
    chunks = [
        _make_recall_chunk("my_skill", status="success", duration=12.0),
        _make_recall_chunk("my_skill", status="failed", duration=8.0, errors=["timeout"]),
        _make_recall_chunk("my_skill", status="success", duration=5.0),
    ]
    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": chunks, "mode": "semantic"},
        }
    }
    result = collect_traces(artifact)

    assert result["data_source"] == "recall"
    assert result["runs_analyzed"] == 3
    assert result["skill_name"] == "my_skill"
    assert result["success_rate"] == pytest.approx(2 / 3)

    md = result["summary_markdown"]
    assert "my_skill" in md
    assert "recall" in md
    assert "## Top error patterns" in md
    assert "## Slowest phases" in md
    assert "## Skill version distribution" in md


# ── Test 4: raw-events fallback when recall empty ────────────────────────────


def test_trace_collector_raw_events_fallback(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: recall empty → falls back to raw .reyn/events/*.jsonl walk."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "runs.jsonl"

    _write_events(log, skill="my_skill", run_id="r1", status="success")
    _write_events(log, skill="my_skill", run_id="r2", status="failed")
    _write_events(log, skill="my_skill", run_id="r3", status="success")

    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": [], "mode": "fallback"},
        }
    }
    result = collect_traces(artifact)

    assert result["data_source"] == "raw_events"
    assert result["runs_analyzed"] == 3
    assert result["skill_name"] == "my_skill"
    assert result["summary_markdown"] != ""


# ── Test 5: empty result when no data ────────────────────────────────────────


def test_trace_collector_empty_when_no_data(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: no recall chunks, no event files → data_source=='empty', runs_analyzed==0."""
    # chdir to tmp_path where no .reyn/events/ exists
    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": [], "mode": "fallback"},
        }
    }
    result = collect_traces(artifact)

    assert result["data_source"] == "empty"
    assert result["runs_analyzed"] == 0
    assert result["skill_name"] == "my_skill"
    # summary_markdown must still be a non-empty string
    assert isinstance(result["summary_markdown"], str)
    assert len(result["summary_markdown"]) > 0


# ── Test 6: filters by skill_name in recall chunks ───────────────────────────


def test_trace_collector_filters_by_skill_name() -> None:
    """Tier 2: recall chunks contain multiple skills; only target skill counted."""
    chunks = [
        _make_recall_chunk("my_skill", status="success"),
        _make_recall_chunk("my_skill", status="failed"),
        _make_recall_chunk("other_skill", status="success"),
        _make_recall_chunk("other_skill", status="success"),
        _make_recall_chunk("other_skill", status="failed"),
    ]
    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": chunks, "mode": "semantic"},
        }
    }
    result = collect_traces(artifact)

    # Only my_skill runs should be counted
    assert result["runs_analyzed"] == 2
    assert result["data_source"] == "recall"
    assert result["skill_name"] == "my_skill"


# ── Test 7: raw-events respects lookback cap ─────────────────────────────────


def test_trace_collector_respects_lookback(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: 30 raw event runs, trace_lookback_runs=10 → only 10 counted."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "runs.jsonl"

    for i in range(30):
        _write_events(
            log,
            skill="my_skill",
            run_id=f"r{i:03d}",
            status="success",
            started_offset_days=float(30 - i),  # oldest first
        )

    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 10,
            "trace_recall_result": None,
        }
    }
    result = collect_traces(artifact)

    assert result["data_source"] == "raw_events"
    assert result["runs_analyzed"] == 10


# ── Test 8: error aggregation sorted by count ────────────────────────────────


def test_trace_collector_aggregates_top_errors(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: runs with error events → top_errors sorted by count descending."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "runs.jsonl"

    # 3 runs with "timeout error"
    for i in range(3):
        _write_events(
            log,
            skill="my_skill",
            run_id=f"timeout_{i}",
            status="failed",
            errors=["test execution timeout"],
        )

    # 1 run with "permission error"
    _write_events(
        log,
        skill="my_skill",
        run_id="perm_1",
        status="failed",
        errors=["PermissionError on write"],
    )

    # 2 runs with "network error"
    for i in range(2):
        _write_events(
            log,
            skill="my_skill",
            run_id=f"net_{i}",
            status="failed",
            errors=["network error: connection refused"],
        )

    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "skill_name": "my_skill",
            "improvement_source": "traces",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": [], "mode": "fallback"},
        }
    }
    result = collect_traces(artifact)

    assert result["data_source"] == "raw_events"
    top_errors = result["top_errors"]
    assert isinstance(top_errors, list)
    assert len(top_errors) > 0

    # top_errors must be sorted by count descending
    counts = [e["count"] for e in top_errors]
    assert counts == sorted(counts, reverse=True), (
        f"top_errors not sorted descending by count: {counts}"
    )

    # "timeout" error should appear first (count=3)
    top_msg = top_errors[0]["msg"]
    assert "timeout" in top_msg.lower(), (
        f"Expected 'timeout' as top error; got: {top_msg!r}"
    )
    assert top_errors[0]["count"] == 3


# ── Tests 9–12: R-PURE-MODE Wave 4 (dispatch_traces / collect_traces_fallback) ─


def test_dispatch_traces_with_recall_chunks_returns_recall_path() -> None:
    """Tier 2: dispatch_traces uses recall chunks when non-empty → _path=recall, stats computed."""
    chunks = [
        _make_recall_chunk("my_skill", status="success", duration=10.0),
        _make_recall_chunk("my_skill", status="failed", duration=5.0, errors=["bad output"]),
        _make_recall_chunk("my_skill", status="success", duration=8.0),
    ]
    artifact = {
        "data": {
            "skill_name": "my_skill",
            "trace_lookback_runs": 20,
            "trace_recall_result": {"chunks": chunks, "mode": "semantic"},
        }
    }
    result = dispatch_traces(artifact)

    assert result["_path"] == "recall"
    assert result["skill_name"] == "my_skill"
    assert result["runs_analyzed"] == 3
    assert result["data_source"] == "recall"
    assert result["success_rate"] == pytest.approx(2 / 3)
    assert isinstance(result["summary_markdown"], str)
    assert "my_skill" in result["summary_markdown"]
    # No fallback-only fields
    assert "target_skill" not in result
    assert "trace_lookback_runs" not in result


def test_dispatch_traces_without_chunks_returns_fallback_sentinel() -> None:
    """Tier 2: dispatch_traces emits needs_fallback sentinel when recall has no chunks."""
    artifact = {
        "data": {
            "skill_name": "my_skill",
            "trace_lookback_runs": 15,
            "trace_recall_result": {"chunks": [], "mode": "fallback"},
        }
    }
    result = dispatch_traces(artifact)

    assert result["_path"] == "needs_fallback"
    assert result["target_skill"] == "my_skill"
    assert result["trace_lookback_runs"] == 15
    # No full stats fields
    assert "runs_analyzed" not in result
    assert "summary_markdown" not in result


def test_collect_traces_fallback_no_ops_when_upstream_recalled() -> None:
    """Tier 2: collect_traces_fallback strips _path sentinel and returns recall stats unchanged."""
    upstream_summary = {
        "_path": "recall",
        "skill_name": "my_skill",
        "runs_analyzed": 5,
        "data_source": "recall",
        "summary_markdown": "# Traces summary for `my_skill`\n\n**Runs analyzed**: 5",
        "success_rate": 0.8,
        "top_errors": [{"phase": "unknown", "msg": "some error", "count": 1}],
    }
    artifact = {
        "data": {
            "skill_name": "my_skill",
            "traces_summary": upstream_summary,
        }
    }
    result = collect_traces_fallback(artifact)

    # Sentinel stripped
    assert "_path" not in result
    # Stats passed through verbatim
    assert result["skill_name"] == "my_skill"
    assert result["runs_analyzed"] == 5
    assert result["data_source"] == "recall"
    assert result["success_rate"] == pytest.approx(0.8)
    assert result["top_errors"] == [{"phase": "unknown", "msg": "some error", "count": 1}]


def test_collect_traces_fallback_walks_raw_events_when_needed(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: collect_traces_fallback walks .reyn/events when traces_summary._path=needs_fallback."""
    events_dir = tmp_path / ".reyn" / "events" / "agents" / "default" / "skill_runs"
    events_dir.mkdir(parents=True)
    log = events_dir / "runs.jsonl"
    _write_events(log, skill="my_skill", run_id="r1", status="success")
    _write_events(log, skill="my_skill", run_id="r2", status="failed")
    _write_events(log, skill="my_skill", run_id="r3", status="success")

    monkeypatch.chdir(tmp_path)

    artifact = {
        "data": {
            "traces_summary": {
                "_path": "needs_fallback",
                "target_skill": "my_skill",
                "trace_lookback_runs": 20,
            }
        }
    }
    result = collect_traces_fallback(artifact)

    assert "_path" not in result
    assert result["data_source"] == "raw_events"
    assert result["runs_analyzed"] == 3
    assert result["skill_name"] == "my_skill"
    assert isinstance(result["summary_markdown"], str)
    assert "raw_events" in result["summary_markdown"]
