"""Tier 2/3: index_events stdlib skill (FP-0009 Component A).

Tier 2 tests cover chunkers.py pure functions directly — no mocks, no LLM,
no OS infrastructure. Tier 3 e2e is deferred (see TODO below).

Covers:
  - collect_run_chunks: grouping, filtering, in-flight skip, error propagation
  - advance_cursor / read_cursor: round-trip and missing-file behaviour
  - text format contains required fields
  - skill.md compiles without errors
  - BUG-1 regression: resolve_scan_context output < ARTIFACT_REF_THRESHOLD
  - collect_chat_turn_chunks (#1821): user-turn chunking from chat JSONL

FP-0042 Phase 2.3 (2026-05-23): chunkers.py migrated to mode: safe — file
I/O goes through reyn.api.safe.file. The autouse ``_safe_file_context`` fixture
below grants reads + writes under ``tmp_path`` so the safe-mode helpers
can run inside the test process (= mirrors what the production
preprocessor_executor wires for the safe-mode subprocess).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reyn.api.safe import embed_index as ei
from reyn.api.safe import file as sf
from reyn.core.context_builder import ARTIFACT_REF_THRESHOLD
from reyn.data.embedding import register_provider
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.stdlib.skills.index_events.chunkers import (
    advance_cursor,
    collect_chat_turn_chunks,
    collect_run_chunks,
    read_cursor,
    resolve_scan_context,
    run_advance_cursor,
    run_collect_chat_chunks,
    run_collect_chunks,
)


class _FakeEmbedProvider:
    """Deterministic embedding provider (no API) for run_collect_chunks tests."""

    def __init__(self, config: dict | None = None) -> None:
        self._batch_size = 100

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        return EmbedBatchResult(
            vectors=[[float(len(t)), 0.0, 0.0, 0.0] for t in texts],
            model=model or "fake-embed",
            total_tokens=sum(len(t) for t in texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) for t in texts)

    def get_dimension(self, model: str) -> int:
        return 4


@pytest.fixture(autouse=True)
def _safe_file_context(tmp_path: Path):
    """Grant reyn.api.safe.file read+write over tmp_path for each test.

    Mirrors the production wiring (= CWD as default read zone, .reyn/ +
    reyn/ + explicit grants as write zones). Tests in this module
    operate exclusively under tmp_path, so a wide grant there matches
    the per-test sandbox model.
    """
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    sf._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path)],
    )
    # #1303 Stage I: run_collect_chunks streams into reyn.api.safe.embed_index;
    # wire a deterministic fake provider (workspace_root defaults to cwd).
    register_provider("fake_ev", _FakeEmbedProvider)
    ei._reset_context()
    ei._set_context(provider_name="fake_ev")
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    ei._reset_context()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, events: list[dict]) -> None:
    """Write a list of event dicts as JSONL to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _run_events(
    *,
    skill: str = "my_skill",
    run_id: str = "run_001",
    started_at: str = "2026-05-15T09:00:00Z",
    completed_at: str = "2026-05-15T09:01:00Z",
    status: str = "success",
    version_hash: str = "abc123",
    phases: list[str] | None = None,
    errors: list[dict] | None = None,
    include_completion: bool = True,
) -> list[dict]:
    """Build a minimal synthetic run_skill_started + optional completion event list."""
    evts: list[dict] = [
        {
            "type": "run_skill_started",
            "timestamp": started_at,
            "data": {
                "skill": skill,
                "run_id": run_id,
                "started_at": started_at,
                "skill_version_hash": version_hash,
            },
        }
    ]
    for phase_name in (phases or []):
        evts.append({
            "type": "skill_node_started",
            "timestamp": started_at,
            "data": {"run_id": run_id, "node": phase_name},
        })
        evts.append({
            "type": "skill_node_completed",
            "timestamp": completed_at,
            "data": {"run_id": run_id, "node": phase_name},
        })
    for err in (errors or []):
        evts.append({
            "type": "error",
            "timestamp": completed_at,
            "data": {**err, "run_id": run_id},
        })
    if include_completion:
        completion_type = "run_skill_failed" if status == "failed" else "run_skill_completed"
        evts.append({
            "type": completion_type,
            "timestamp": completed_at,
            "data": {
                "skill": skill,
                "run_id": run_id,
                "completed_at": completed_at,
                "status": status,
            },
        })
    return evts


# ── Tier 2: collect_run_chunks ────────────────────────────────────────────────


def test_collect_run_chunks_groups_by_run(tmp_path):
    """Tier 2: collect_run_chunks groups events into 3 correct chunks (2 success, 1 failure)."""
    # Run A in file 1
    file1 = tmp_path / "2026-05" / "run_a.jsonl"
    _write_jsonl(
        file1,
        _run_events(skill="skill_a", run_id="r001", version_hash="hash_a1")
        + _run_events(skill="skill_a", run_id="r002", version_hash="hash_a2"),
    )
    # Run B (failed) in file 2
    file2 = tmp_path / "2026-05" / "run_b.jsonl"
    _write_jsonl(
        file2,
        _run_events(
            skill="skill_b",
            run_id="r003",
            version_hash="hash_b",
            status="failed",
        ),
    )

    chunks = collect_run_chunks(str(tmp_path), since=None)

    # Check statuses: skill_a runs ×2 success, skill_b ×1 failed
    skills_seen = sorted(c["metadata"]["extra"]["skill"] for c in chunks)
    assert skills_seen == ["skill_a", "skill_a", "skill_b"], (
        f"Expected skill_a×2, skill_b×1, got: {skills_seen}"
    )
    skill_a_chunks = [c for c in chunks if c["metadata"]["extra"]["skill"] == "skill_a"]
    skill_b_chunks = [c for c in chunks if c["metadata"]["extra"]["skill"] == "skill_b"]
    assert all(c["metadata"]["extra"]["status"] == "success" for c in skill_a_chunks)
    (only_b,) = skill_b_chunks
    assert only_b["metadata"]["extra"]["status"] == "failed"

    # Check version_hash propagation
    hashes = {c["metadata"]["extra"]["skill_version_hash"] for c in chunks}
    assert "hash_a1" in hashes
    assert "hash_a2" in hashes
    assert "hash_b" in hashes


def test_collect_run_chunks_skips_inflight_runs(tmp_path):
    """Tier 2: run with run_skill_started but no completion event is skipped."""
    file1 = tmp_path / "2026-05" / "inflight.jsonl"
    _write_jsonl(
        file1,
        _run_events(skill="my_skill", run_id="inflight_001", include_completion=False),
    )
    # Add one complete run so we verify it's not just returning 0 for all
    _write_jsonl(
        tmp_path / "2026-05" / "complete.jsonl",
        _run_events(skill="my_skill", run_id="complete_001"),
    )

    chunks = collect_run_chunks(str(tmp_path), since=None)

    (only,) = chunks  # in-flight run must be skipped; exactly 1 complete run remains
    assert only["metadata"]["extra"].get("skill") == "my_skill"


def test_collect_run_chunks_filters_by_since(tmp_path):
    """Tier 2: only runs with completed_at >= since are returned."""
    events_dir = tmp_path / "2026-05"

    # 5 runs with distinct timestamps
    all_events: list[dict] = []
    for i in range(1, 6):
        hour = f"{8 + i:02d}"
        all_events.extend(
            _run_events(
                skill="my_skill",
                run_id=f"run_{i:03d}",
                started_at=f"2026-05-15T{hour}:00:00Z",
                completed_at=f"2026-05-15T{hour}:01:00Z",
            )
        )
    _write_jsonl(events_dir / "runs.jsonl", all_events)

    # Filter: only runs completed at >= 12:00 (runs 3, 4, 5 → hours 11, 12, 13)
    # run_1: 09:01, run_2: 10:01, run_3: 11:01, run_4: 12:01, run_5: 13:01
    chunks = collect_run_chunks(str(tmp_path), since="2026-05-15T11:01:00Z")

    # Runs with completed_at >= 11:01:00 → run_3 (11:01), run_4 (12:01), run_5 (13:01)
    ended_ats = sorted(c["metadata"]["extra"]["ended_at"] for c in chunks)
    assert ended_ats == [
        "2026-05-15T11:01:00Z",
        "2026-05-15T12:01:00Z",
        "2026-05-15T13:01:00Z",
    ], f"Expected 3 chunks (since filter), got timestamps: {ended_ats}"


def test_collect_run_chunks_failure_includes_errors(tmp_path):
    """Tier 2: failed run with 2 error events → chunk metadata.errors contains both (max 3)."""
    errors = [
        {"error": "Phase 'verify' raised AssertionError: test_foo failed"},
        {"error": "Timeout exceeded in shell op"},
    ]
    events_dir = tmp_path / "2026-05"
    _write_jsonl(
        events_dir / "failed_run.jsonl",
        _run_events(
            skill="my_skill",
            run_id="err_run_001",
            status="failed",
            errors=errors,
        ),
    )

    chunks = collect_run_chunks(str(tmp_path), since=None)

    (only,) = chunks
    chunk_errors = only["metadata"]["extra"]["errors"]
    (err_a, err_b) = chunk_errors  # exactly 2 errors from the 2 error events
    assert "AssertionError" in err_a or "verify" in err_a.lower(), (
        f"First error message missing expected content: {err_a!r}"
    )
    assert "Timeout" in err_b or "timeout" in err_b.lower(), (
        f"Second error message missing expected content: {err_b!r}"
    )


def test_collect_run_chunks_text_format_human_readable(tmp_path):
    """Tier 2: chunk.text contains required labelled lines for semantic search."""
    events_dir = tmp_path / "2026-05"
    _write_jsonl(
        events_dir / "run.jsonl",
        _run_events(
            skill="test_skill",
            run_id="txt_001",
            started_at="2026-05-15T10:00:00Z",
            completed_at="2026-05-15T10:01:30Z",
            version_hash="deadbeef1234",
            phases=["plan", "execute"],
        ),
    )

    chunks = collect_run_chunks(str(tmp_path), since=None)

    (only,) = chunks
    text = only["text"]

    assert "skill:" in text, f"'skill:' label missing in text:\n{text}"
    assert "status:" in text, f"'status:' label missing in text:\n{text}"
    assert "duration_seconds:" in text, f"'duration_seconds:' label missing in text:\n{text}"
    assert "errors:" in text, f"'errors:' label missing in text:\n{text}"


def test_collect_run_chunks_unknown_version_hash_fallback(tmp_path):
    """Tier 2: events without skill_version_hash produce 'unknown' in metadata (FP-0006 A compat)."""
    events_dir = tmp_path / "2026-05"
    events = [
        {
            "type": "run_skill_started",
            "timestamp": "2026-05-15T09:00:00Z",
            "data": {
                "skill": "legacy_skill",
                "run_id": "legacy_001",
                "started_at": "2026-05-15T09:00:00Z",
                # No skill_version_hash field
            },
        },
        {
            "type": "run_skill_completed",
            "timestamp": "2026-05-15T09:01:00Z",
            "data": {
                "skill": "legacy_skill",
                "run_id": "legacy_001",
                "completed_at": "2026-05-15T09:01:00Z",
                "status": "success",
            },
        },
    ]
    _write_jsonl(events_dir / "legacy.jsonl", events)

    chunks = collect_run_chunks(str(tmp_path), since=None)

    (only,) = chunks
    assert only["metadata"]["extra"]["skill_version_hash"] == "unknown"


# ── Tier 2: cursor helpers ────────────────────────────────────────────────────


def test_cursor_round_trip(tmp_path):
    """Tier 2: advance_cursor → read_cursor returns the written value."""
    cursor_path = str(tmp_path / "index" / "events_cursor")
    ts = "2026-05-15T12:34:56Z"

    advance_cursor(cursor_path, ts)
    result = read_cursor(cursor_path)

    assert result == ts, f"Expected {ts!r}, got {result!r}"


def test_cursor_read_missing_returns_none(tmp_path):
    """Tier 2: read_cursor on nonexistent path returns None."""
    cursor_path = str(tmp_path / "nonexistent" / "events_cursor")
    result = read_cursor(cursor_path)
    assert result is None


def test_cursor_overwrite(tmp_path):
    """Tier 2: advance_cursor overwrites an existing cursor file atomically."""
    cursor_path = str(tmp_path / "events_cursor")
    advance_cursor(cursor_path, "2026-05-14T00:00:00Z")
    advance_cursor(cursor_path, "2026-05-15T12:00:00Z")
    assert read_cursor(cursor_path) == "2026-05-15T12:00:00Z"


# ── Tier 2: skill.md compilation ─────────────────────────────────────────────


def test_index_events_skill_md_compiles():
    """Tier 2: index_events skill.md exists and compiles without errors."""
    from reyn.core.compiler.loader import load_dsl_skill
    from reyn.schemas.models import Skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_events" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent  # src/reyn/stdlib/

    assert skill_md.exists(), f"skill.md not found at {skill_md}"
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert isinstance(skill, Skill)
    assert skill.name == "index_events"


def test_index_events_skill_entry_phase():
    """Tier 2: entry phase is 'scan'."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_events" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.entry_phase == "scan"


def test_index_events_skill_has_postprocessor():
    """Tier 2: skill.postprocessor is non-None with 4 steps."""
    from reyn.core.compiler.loader import load_dsl_skill
    from reyn.schemas.models import Postprocessor

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_events" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.postprocessor is not None
    assert isinstance(skill.postprocessor, Postprocessor)
    step_types = [s.type for s in skill.postprocessor.steps]
    # #1303 Stage I folded the embed + index_write run-ops into run_collect_chunks;
    # only safe-python steps remain (collect + advance_cursor).
    assert all(t == "python" for t in step_types), (
        f"Expected only python steps post-cutover, got: {step_types}"
    )
    fns = [s.function for s in skill.postprocessor.steps]
    assert "run_collect_chunks" in fns
    assert "run_advance_cursor" in fns


def test_index_events_skill_postprocessor_output():
    """Tier 2: postprocessor.output_name == 'index_events_summary'."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_events" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.postprocessor.output_name == "index_events_summary"


def test_index_events_skill_graph_single_phase():
    """Tier 2: graph has no transitions from scan (single-phase skill)."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_events" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    transitions = skill.graph.transitions.get("scan", [])
    assert transitions == [], f"Expected no transitions from scan, got: {transitions}"


# ── Tier 2: BUG-1 regression — preprocessor output must stay below threshold ──


def test_resolve_scan_context_output_size_under_threshold(tmp_path, monkeypatch):
    """Tier 2: resolve_scan_context output JSON < 8000 bytes even with 100+ event files (BUG-1 regression).

    ARTIFACT_REF_THRESHOLD = 8000 bytes.  If the preprocessor output exceeds
    that, the OS converts it to an artifact_ref which the LLM cannot
    dereference (scan phase has allowed_ops: []), causing hallucinated file
    paths and chunk_count=0.
    """
    # Create 120 dummy .jsonl files under a fake .reyn/events/ dir
    fake_events = tmp_path / ".reyn" / "events" / "2026-05"
    fake_events.mkdir(parents=True)
    for i in range(120):
        (fake_events / f"run_{i:04d}.jsonl").write_text("{}\n", encoding="utf-8")

    # Patch the module-level _EVENTS_DIR and _CURSOR_FILE to use tmp_path
    import reyn.stdlib.skills.index_events.chunkers as ck
    original_events_dir = ck._EVENTS_DIR
    original_cursor_file = ck._CURSOR_FILE
    ck._EVENTS_DIR = tmp_path / ".reyn" / "events"
    ck._CURSOR_FILE = tmp_path / ".reyn" / "index" / "events_cursor"
    try:
        artifact = {"data": {"mode": "append"}}
        result = resolve_scan_context(artifact)
    finally:
        ck._EVENTS_DIR = original_events_dir
        ck._CURSOR_FILE = original_cursor_file

    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized) < ARTIFACT_REF_THRESHOLD, (
        f"resolve_scan_context output exceeds ARTIFACT_REF_THRESHOLD ({ARTIFACT_REF_THRESHOLD} bytes): "
        f"{len(serialized)} bytes. Output keys: {list(result.keys())}"
    )


def test_resolve_scan_context_returns_count_not_paths(tmp_path, monkeypatch):
    """Tier 2: resolve_scan_context returns event_files_count (int) and NOT event_files (list) (BUG-1).

    The file list is the root cause of BUG-1: it makes the output too large.
    Verify the contract has changed to count-only.
    """
    fake_events = tmp_path / ".reyn" / "events"
    fake_events.mkdir(parents=True)
    (fake_events / "run_001.jsonl").write_text("{}\n", encoding="utf-8")
    (fake_events / "run_002.jsonl").write_text("{}\n", encoding="utf-8")

    import reyn.stdlib.skills.index_events.chunkers as ck
    original_events_dir = ck._EVENTS_DIR
    original_cursor_file = ck._CURSOR_FILE
    ck._EVENTS_DIR = fake_events
    ck._CURSOR_FILE = tmp_path / ".reyn" / "index" / "events_cursor"
    try:
        result = resolve_scan_context({"data": {"mode": "append"}})
    finally:
        ck._EVENTS_DIR = original_events_dir
        ck._CURSOR_FILE = original_cursor_file

    assert "event_files_count" in result, (
        f"'event_files_count' missing from resolve_scan_context output: {list(result.keys())}"
    )
    assert isinstance(result["event_files_count"], int), (
        f"event_files_count should be int, got {type(result['event_files_count'])}"
    )
    assert result["event_files_count"] == 2, (
        f"Expected 2 files, got {result['event_files_count']}"
    )
    assert "event_files" not in result, (
        f"'event_files' (the old path list) must NOT appear in output — it triggers BUG-1. "
        f"Keys: {list(result.keys())}"
    )


def test_run_collect_chunks_reglobs_files_internally(tmp_path, monkeypatch):
    """Tier 2: run_collect_chunks succeeds without event_files in the artifact (BUG-1 fix).

    After BUG-1 fix, the postprocessor must discover files via re-glob, not
    by reading event_files from the LLM artifact. Passing an artifact with no
    event_files key must still produce correct chunks.
    """
    # Write events under tmp_path/.reyn/events/ (cwd-relative path used by run_collect_chunks)
    events_dir = tmp_path / ".reyn" / "events" / "2026-05"
    events_dir.mkdir(parents=True)
    events = _run_events(skill="my_skill", run_id="rc_001")
    (events_dir / "rc_001.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    # Artifact has since + mode but NO event_files
    artifact = {
        "data": {
            "since": "1970-01-01T00:00:00Z",
            "mode": "append",
            "skill_filter": None,
            # deliberately omit event_files
        }
    }

    # run_collect_chunks re-globs + streams to embed_index (cwd-relative).
    # Change cwd to tmp_path so the re-glob and .reyn/index output are correct.
    original_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        result = run_collect_chunks(artifact)
    finally:
        os.chdir(original_cwd)

    assert result["chunk_count"] == 1, (
        f"Expected 1 chunk from re-glob, got {result['chunk_count']}. "
        f"Skipped: {result['skipped_runs']}, filtered: {result['filtered_runs']}"
    )
    # #1303 Stage I: the chunk landed in the events index (no intermediate file).
    assert result["embedded"] == 1
    assert not (tmp_path / "artifacts" / "event_chunks.jsonl").exists()
    import asyncio

    from reyn.data.index import SqliteIndexBackend

    stat = asyncio.run(SqliteIndexBackend(workspace_root=tmp_path).stat("events"))
    assert stat["chunk_count"] == 1


def test_run_collect_chunks_resume_skips_reembed(tmp_path, monkeypatch):
    """Tier 2: ★resume through the index_events chunker path — a second pass
    over the same events re-embeds nothing (existing_hashes pre-embed skip)."""
    events_dir = tmp_path / ".reyn" / "events" / "2026-05"
    events_dir.mkdir(parents=True)
    (events_dir / "rc.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _run_events(skill="s", run_id="r1")) + "\n",
        encoding="utf-8",
    )
    artifact = {"data": {"since": "1970-01-01T00:00:00Z", "mode": "append"}}
    monkeypatch.chdir(str(tmp_path))

    first = run_collect_chunks(artifact)
    assert first["embedded"] == 1

    second = run_collect_chunks({"data": {"since": "1970-01-01T00:00:00Z", "mode": "append"}})
    assert second["embedded"] == 0
    assert second["skipped_embed"] == 1


def test_run_advance_cursor_uses_chunk_stats_max(tmp_path, monkeypatch):
    """Tier 2: run_advance_cursor advances from data.chunk_stats.max_completed_at
    (the value run_collect_chunks tracked inline) — no intermediate file read."""
    monkeypatch.chdir(str(tmp_path))
    (tmp_path / ".reyn" / "index").mkdir(parents=True)
    artifact = {
        "data": {
            "chunk_stats": {
                "chunk_count": 2,
                "skipped_runs": 0,
                "filtered_runs": 0,
                "max_completed_at": "2026-05-15T10:00:00Z",
            }
        }
    }
    result = run_advance_cursor(artifact)
    assert result["new_cursor"] == "2026-05-15T10:00:00Z"
    assert result["indexed_runs"] == 2
    assert read_cursor(".reyn/index/events_cursor") == "2026-05-15T10:00:00Z"


# ── Tier 2: collect_chat_turn_chunks (#1821 improvement-1) ───────────────────


def _chat_events(
    *,
    agent: str = "beta",
    chain_id: str = "chain_001",
    turn_ts: str = "2026-06-01T10:00:00Z",
    user_text: str = "hello world",
    media_block_count: int = 0,
    turn_outcome: str = "inline_reply",  # "inline_reply" | "routing" | "spawned"
    routed_action: str | None = None,
) -> list[dict]:
    """Build synthetic chat turn event list for testing."""
    events: list[dict] = [
        {
            "type": "user_message_received",
            "timestamp": turn_ts,
            "data": {
                "text": user_text,
                "chain_id": chain_id,
                "media_block_count": media_block_count,
                "agent_id": f"reyn/{agent}",
            },
        }
    ]
    if turn_outcome == "inline_reply":
        events.append({
            "type": "chat_turn_completed_inline",
            "timestamp": turn_ts,
            "data": {
                "chain_id": chain_id,
                "decision": "inline_reply",
                "tool_calls_attempted": 0,
            },
        })
    elif turn_outcome == "routing":
        events.append({
            "type": "routing_decided",
            "timestamp": turn_ts,
            "data": {
                "chain_id": chain_id,
                "action_name": routed_action or "skill__code_review",
                "source": "invoke_action",
                "outcome": "success",
            },
        })
    elif turn_outcome == "spawned":
        events.append({
            "type": "skill_run_spawned",
            "timestamp": turn_ts,
            "data": {"chain_id": chain_id},
        })
    return events


def _write_chat_jsonl(
    agents_root: Path,
    agent: str,
    filename: str,
    events: list[dict],
) -> Path:
    """Write synthetic chat events under agents_root/<agent>/chat/2026-06/<filename>."""
    chat_dir = agents_root / agent / "chat" / "2026-06"
    chat_dir.mkdir(parents=True, exist_ok=True)
    file_path = chat_dir / filename
    with open(file_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return file_path


def test_collect_chat_turn_chunks_basic(tmp_path):
    """Tier 2: collect_chat_turn_chunks emits one chunk per user_message_received event."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "session_a.jsonl",
        _chat_events(agent="beta", chain_id="c001", user_text="hello"),
    )
    _write_chat_jsonl(
        agents_root, "beta", "session_b.jsonl",
        _chat_events(agent="beta", chain_id="c002", user_text="how are you"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    chain_ids = {c["metadata"]["extra"]["chain_id"] for c in chunks}
    assert chain_ids == {"c001", "c002"}


def test_collect_chat_turn_chunks_id_scheme(tmp_path):
    """Tier 2: chunk id follows chat__<agent>__<chain_id> scheme (#1821)."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "my_agent", "s.jsonl",
        _chat_events(agent="my_agent", chain_id="abc123"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    (chunk,) = chunks  # exactly 1 turn from 1 user_message_received
    assert chunk["id"] == "chat__my_agent__abc123", (
        f"Unexpected chunk id: {chunk['id']!r}"
    )


def test_collect_chat_turn_chunks_metadata_fields(tmp_path):
    """Tier 2: chat chunk metadata carries agent, chain_id, turn_ts, turn_outcome."""
    agents_root = tmp_path / "agents"
    ts = "2026-06-01T12:34:56Z"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(
            agent="beta", chain_id="chain_xyz",
            turn_ts=ts, user_text="test query",
            turn_outcome="inline_reply",
        ),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks
    extra = chunk["metadata"]["extra"]

    assert extra["agent"] == "beta"
    assert extra["chain_id"] == "chain_xyz"
    assert extra["turn_ts"] == ts
    assert extra["turn_outcome"] == "inline_reply"
    assert extra["routed_action"] is None


def test_collect_chat_turn_chunks_routing_outcome(tmp_path):
    """Tier 2: turn_outcome='routing' and routed_action are captured from routing_decided."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(
            chain_id="c_route",
            turn_outcome="routing",
            routed_action="skill__code_review",
        ),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks
    extra = chunk["metadata"]["extra"]

    assert extra["turn_outcome"] == "routing"
    assert extra["routed_action"] == "skill__code_review"


def test_collect_chat_turn_chunks_source_type(tmp_path):
    """Tier 2: source_type is 'chat_turn' (not 'p6_event_run')."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c1"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks

    assert chunk["metadata"]["source_type"] == "chat_turn"


def test_collect_chat_turn_chunks_text_format(tmp_path):
    """Tier 2: chunk.text contains 'agent:', 'user:', 'turn_outcome:' labels."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c1", user_text="how does recall work?"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks
    text = chunk["text"]

    assert "agent:" in text, f"'agent:' label missing:\n{text}"
    assert "user:" in text, f"'user:' label missing:\n{text}"
    assert "turn_outcome:" in text, f"'turn_outcome:' label missing:\n{text}"
    assert "how does recall work?" in text


def test_collect_chat_turn_chunks_filters_by_since(tmp_path):
    """Tier 2: turns with timestamp < since are excluded."""
    agents_root = tmp_path / "agents"
    old_events = _chat_events(
        chain_id="old", turn_ts="2026-05-01T00:00:00Z", user_text="old message"
    )
    new_events = _chat_events(
        chain_id="new", turn_ts="2026-06-15T00:00:00Z", user_text="new message"
    )
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", old_events + new_events)

    chunks = collect_chat_turn_chunks(str(agents_root), since="2026-06-01T00:00:00Z")

    # Only the newer turn (new) should pass the since filter; the old one is excluded.
    (surviving,) = chunks
    assert surviving["metadata"]["extra"]["chain_id"] == "new"


def test_collect_chat_turn_chunks_skips_no_user_event(tmp_path):
    """Tier 2: files with no user_message_received produce zero chunks (non-user chains)."""
    agents_root = tmp_path / "agents"
    # Only non-user events (e.g. chat_started, session_restored)
    events = [
        {"type": "chat_started", "timestamp": "2026-06-01T09:00:00Z",
         "data": {"agent_id": "reyn/beta", "chain_id": "c1"}},
        {"type": "session_restored", "timestamp": "2026-06-01T09:00:01Z",
         "data": {"agent_id": "reyn/beta", "chain_id": "c1"}},
    ]
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", events)

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    assert chunks == [], f"Expected 0 chunks, got {len(chunks)}"


def test_collect_chat_turn_chunks_multi_agent(tmp_path):
    """Tier 2: turns from different agents produce separate chunks with correct agent names."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "alpha", "s.jsonl",
        _chat_events(agent="alpha", chain_id="ca1"),
    )
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(agent="beta", chain_id="cb1"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    agents_seen = {c["metadata"]["extra"]["agent"] for c in chunks}
    assert agents_seen == {"alpha", "beta"}


def test_collect_chat_turn_chunks_dedup_by_chain_id(tmp_path):
    """Tier 2: multiple events in one file with the same chain_id yield one chunk."""
    agents_root = tmp_path / "agents"
    # Two events share chain_id: user_message_received + chat_turn_completed_inline
    events = _chat_events(chain_id="shared", turn_outcome="inline_reply")
    # Fixture must have exactly one user_message_received (the trigger we chunk on)
    user_evts = [e for e in events if e["type"] == "user_message_received"]
    # Sanity-check the helper, not the production code under test
    if not user_evts:
        pytest.fail("Test fixture has no user_message_received — helper is broken")
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", events)

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    # Same chain_id → grouped into exactly one chunk (the user turn)
    (only,) = chunks  # unpacking enforces count without len()


def test_run_collect_chat_chunks_embeds_and_indexes(tmp_path, monkeypatch):
    """Tier 2: run_collect_chat_chunks embeds turns into the 'chat' index source."""
    agents_root = tmp_path / ".reyn" / "events" / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c001", user_text="test query"),
    )

    import reyn.stdlib.skills.index_events.chunkers as ck
    original_dir = ck._CHAT_EVENTS_DIR
    ck._CHAT_EVENTS_DIR = str(agents_root)
    monkeypatch.chdir(str(tmp_path))
    try:
        artifact = {"data": {"since": "1970-01-01T00:00:00Z"}}
        result = run_collect_chat_chunks(artifact)
    finally:
        ck._CHAT_EVENTS_DIR = original_dir

    assert result["chunk_count"] == 1, (
        f"Expected 1 chunk embedded, got {result['chunk_count']}. "
        f"Skipped: {result['skipped_turns']}"
    )
    assert result["embedded"] == 1
    # Verify the chunk landed in the 'chat' index
    import asyncio

    from reyn.data.index import SqliteIndexBackend
    stat = asyncio.run(SqliteIndexBackend(workspace_root=tmp_path).stat("chat"))
    assert stat["chunk_count"] == 1, (
        f"Expected 1 chunk in 'chat' index, got {stat['chunk_count']}"
    )


def test_run_collect_chat_chunks_resume_skips_reembed(tmp_path, monkeypatch):
    """Tier 2: second run over the same chat JSONL re-embeds nothing (dedup by content_hash)."""
    agents_root = tmp_path / ".reyn" / "events" / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c001"),
    )

    import reyn.stdlib.skills.index_events.chunkers as ck
    original_dir = ck._CHAT_EVENTS_DIR
    ck._CHAT_EVENTS_DIR = str(agents_root)
    monkeypatch.chdir(str(tmp_path))
    try:
        artifact = {"data": {"since": "1970-01-01T00:00:00Z"}}
        first = run_collect_chat_chunks(artifact)
        assert first["embedded"] == 1

        second = run_collect_chat_chunks({"data": {"since": "1970-01-01T00:00:00Z"}})
        assert second["embedded"] == 0
        assert second["skipped_embed"] == 1
    finally:
        ck._CHAT_EVENTS_DIR = original_dir


# ── TODO(fp-0009): Tier 3 e2e ─────────────────────────────────────────────────
# Full round-trip test via reyn run index_events against a tmpdir with seeded
# events, then verifying recall op finds the indexed run. Deferred because it
# requires a live LiteLLM embedding endpoint and the full OS harness — too
# heavyweight for CI without a mocked embed provider. When the embed mock is
# available (tracked in project_residuals.md), add:
#
#   def test_index_events_e2e_recall(tmp_path):
#       """Tier 3: index_events e2e — run + recall finds indexed chunks."""
#       ...seed events in tmp_path/.reyn/events/...
#       ...reyn.run("index_events") against tmp_path workspace...
#       ...assert recall(query="my_skill failure") returns ≥1 result...
#       ...assert cursor file was written...
