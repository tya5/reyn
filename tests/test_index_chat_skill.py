"""Tier 2: index_chat stdlib skill (#1821 improvement-1 — wiring).

Tests cover:
  - skill.md compilation (schema, entry phase, postprocessor, graph)
  - chunkers.py pure functions: collect_chat_turn_chunks, advance_chat_cursor,
    read_chat_cursor, resolve_chat_scan_context
  - postprocessor entry points: run_collect_chat_chunks, run_advance_chat_cursor
    (embed + index in 'chat' source, cursor advance, incremental resume)

FP-0042: chunkers.py uses mode: safe — file I/O through reyn.api.safe.file.
The autouse ``_safe_file_context`` fixture grants reads + writes under
``tmp_path``, mirroring the production preprocessor_executor wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.api.safe import embed_index as ei
from reyn.api.safe import file as sf
from reyn.data.embedding import register_provider
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.stdlib.skills.index_chat.chunkers import (
    advance_chat_cursor,
    collect_chat_turn_chunks,
    read_chat_cursor,
    resolve_chat_scan_context,
    run_advance_chat_cursor,
    run_collect_chat_chunks,
)


class _FakeEmbedProvider:
    """Deterministic embedding provider (no API) for postprocessor tests."""

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
    """Grant reyn.api.safe.file read+write over tmp_path for each test."""
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    sf._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path)],
    )
    register_provider("fake_ic", _FakeEmbedProvider)
    ei._reset_context()
    ei._set_context(provider_name="fake_ic")
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    ei._reset_context()


# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _chat_events(
    *,
    chain_id: str = "chain_001",
    turn_ts: str = "2026-06-01T10:00:00Z",
    user_text: str = "hello world",
    turn_outcome: str = "inline_reply",
    routed_action: str | None = None,
) -> list[dict]:
    """Build minimal synthetic chat turn event list."""
    events: list[dict] = [
        {
            "type": "user_message_received",
            "timestamp": turn_ts,
            "data": {
                "text": user_text,
                "chain_id": chain_id,
                "media_block_count": 0,
            },
        }
    ]
    if turn_outcome == "inline_reply":
        events.append({
            "type": "chat_turn_completed_inline",
            "timestamp": turn_ts,
            "data": {"chain_id": chain_id},
        })
    elif turn_outcome == "routing":
        events.append({
            "type": "routing_decided",
            "timestamp": turn_ts,
            "data": {
                "chain_id": chain_id,
                "action_name": routed_action or "skill__review",
            },
        })
    return events


# ── Tier 2: skill.md compilation ─────────────────────────────────────────────


def test_index_chat_skill_md_compiles():
    """Tier 2: index_chat skill.md exists and compiles without errors."""
    from reyn.core.compiler.loader import load_dsl_skill
    from reyn.schemas.models import Skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_chat" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent  # src/reyn/stdlib/

    assert skill_md.exists(), f"skill.md not found at {skill_md}"
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert isinstance(skill, Skill)
    assert skill.name == "index_chat"


def test_index_chat_entry_phase():
    """Tier 2: entry phase is 'scan'."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_chat" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.entry_phase == "scan"


def test_index_chat_has_postprocessor():
    """Tier 2: postprocessor has run_collect_chat_chunks and run_advance_chat_cursor steps."""
    from reyn.core.compiler.loader import load_dsl_skill
    from reyn.schemas.models import Postprocessor

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_chat" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.postprocessor is not None
    assert isinstance(skill.postprocessor, Postprocessor)
    fns = [s.function for s in skill.postprocessor.steps]
    assert "run_collect_chat_chunks" in fns
    assert "run_advance_chat_cursor" in fns
    assert all(s.type == "python" for s in skill.postprocessor.steps)


def test_index_chat_postprocessor_output_name():
    """Tier 2: postprocessor.output_name == 'index_chat_summary'."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_chat" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    assert skill.postprocessor.output_name == "index_chat_summary"


def test_index_chat_graph_single_phase():
    """Tier 2: graph has no transitions from scan (single-phase skill)."""
    from reyn.core.compiler.loader import load_dsl_skill

    skill_md = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_chat" / "skill.md"
    )
    skill_root = skill_md.parent.parent.parent
    skill = load_dsl_skill(skill_md, skill_root=skill_root)
    transitions = skill.graph.transitions.get("scan", [])
    assert transitions == [], f"Expected no transitions from scan, got: {transitions}"


# ── Tier 2: collect_chat_turn_chunks ─────────────────────────────────────────


def test_collect_chat_turn_chunks_basic(tmp_path):
    """Tier 2: collect_chat_turn_chunks emits one chunk per user_message_received event."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(agents_root, "beta", "a.jsonl", _chat_events(chain_id="c001"))
    _write_chat_jsonl(agents_root, "beta", "b.jsonl", _chat_events(chain_id="c002"))

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    chain_ids = {c["metadata"]["extra"]["chain_id"] for c in chunks}
    assert chain_ids == {"c001", "c002"}


def test_collect_chat_turn_chunks_id_scheme(tmp_path):
    """Tier 2: chunk id follows chat__<agent>__<chain_id> scheme."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "my_agent", "s.jsonl",
        _chat_events(chain_id="abc123"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)

    (chunk,) = chunks
    assert chunk["id"] == "chat__my_agent__abc123", (
        f"Unexpected chunk id: {chunk['id']!r}"
    )


def test_collect_chat_turn_chunks_metadata_fields(tmp_path):
    """Tier 2: metadata carries agent, chain_id, turn_ts, turn_outcome, source_type."""
    agents_root = tmp_path / "agents"
    ts = "2026-06-10T12:00:00Z"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="xyz", turn_ts=ts, turn_outcome="inline_reply"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks
    extra = chunk["metadata"]["extra"]

    assert extra["agent"] == "beta"
    assert extra["chain_id"] == "xyz"
    assert extra["turn_ts"] == ts
    assert extra["turn_outcome"] == "inline_reply"
    assert extra["routed_action"] is None
    assert chunk["metadata"]["source_type"] == "chat_turn"


def test_collect_chat_turn_chunks_routing_outcome(tmp_path):
    """Tier 2: routing turn_outcome and routed_action are captured from routing_decided."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c_route", turn_outcome="routing", routed_action="skill__review"),
    )

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    (chunk,) = chunks
    extra = chunk["metadata"]["extra"]

    assert extra["turn_outcome"] == "routing"
    assert extra["routed_action"] == "skill__review"


def test_collect_chat_turn_chunks_filters_by_since(tmp_path):
    """Tier 2: turns with timestamp < since are excluded."""
    agents_root = tmp_path / "agents"
    old_events = _chat_events(chain_id="old", turn_ts="2026-05-01T00:00:00Z")
    new_events = _chat_events(chain_id="new", turn_ts="2026-06-15T00:00:00Z")
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", old_events + new_events)

    chunks = collect_chat_turn_chunks(str(agents_root), since="2026-06-01T00:00:00Z")

    (surviving,) = chunks
    assert surviving["metadata"]["extra"]["chain_id"] == "new"


def test_collect_chat_turn_chunks_skips_no_user_event(tmp_path):
    """Tier 2: files with no user_message_received produce zero chunks."""
    agents_root = tmp_path / "agents"
    events = [
        {"type": "chat_started", "timestamp": "2026-06-01T09:00:00Z",
         "data": {"chain_id": "c1"}},
    ]
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", events)

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    assert chunks == []


def test_collect_chat_turn_chunks_multi_agent(tmp_path):
    """Tier 2: turns from different agents produce separate chunks with correct agent names."""
    agents_root = tmp_path / "agents"
    _write_chat_jsonl(agents_root, "alpha", "s.jsonl", _chat_events(chain_id="ca1"))
    _write_chat_jsonl(agents_root, "beta", "s.jsonl", _chat_events(chain_id="cb1"))

    chunks = collect_chat_turn_chunks(str(agents_root), since=None)
    agents_seen = {c["metadata"]["extra"]["agent"] for c in chunks}
    assert agents_seen == {"alpha", "beta"}


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

    assert "agent:" in text
    assert "user:" in text
    assert "turn_outcome:" in text
    assert "how does recall work?" in text


# ── Tier 2: cursor helpers ────────────────────────────────────────────────────


def test_chat_cursor_round_trip(tmp_path):
    """Tier 2: advance_chat_cursor → read_chat_cursor returns the written value."""
    cursor_path = str(tmp_path / "index" / "chat_cursor")
    ts = "2026-06-15T12:34:56Z"

    advance_chat_cursor(cursor_path, ts)
    result = read_chat_cursor(cursor_path)

    assert result == ts, f"Expected {ts!r}, got {result!r}"


def test_chat_cursor_read_missing_returns_none(tmp_path):
    """Tier 2: read_chat_cursor on nonexistent path returns None."""
    result = read_chat_cursor(str(tmp_path / "nonexistent" / "chat_cursor"))
    assert result is None


def test_chat_cursor_overwrite(tmp_path):
    """Tier 2: advance_chat_cursor overwrites an existing cursor file atomically."""
    cursor_path = str(tmp_path / "chat_cursor")
    advance_chat_cursor(cursor_path, "2026-06-01T00:00:00Z")
    advance_chat_cursor(cursor_path, "2026-06-15T12:00:00Z")
    assert read_chat_cursor(cursor_path) == "2026-06-15T12:00:00Z"


# ── Tier 2: resolve_chat_scan_context ────────────────────────────────────────


def test_resolve_chat_scan_context_no_files(tmp_path, monkeypatch):
    """Tier 2: resolve_chat_scan_context returns count=0 when no chat files exist."""
    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    orig_cursor = ck._CURSOR_FILE
    ck._CHAT_EVENTS_DIR = str(tmp_path / ".reyn" / "events" / "agents")
    ck._CURSOR_FILE = str(tmp_path / ".reyn" / "index" / "chat_cursor")
    try:
        result = resolve_chat_scan_context({"data": {"mode": "append"}})
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir
        ck._CURSOR_FILE = orig_cursor

    assert result["chat_files_count"] == 0
    assert result["since"] == "1970-01-01T00:00:00Z"
    assert result["cursor_exists"] is False
    assert result["mode"] == "append"


def test_resolve_chat_scan_context_reads_cursor(tmp_path, monkeypatch):
    """Tier 2: resolve_chat_scan_context reads .reyn/index/chat_cursor when present."""
    cursor_ts = "2026-06-10T09:00:00Z"
    cursor_path = tmp_path / ".reyn" / "index" / "chat_cursor"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text(cursor_ts, encoding="utf-8")

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    orig_cursor = ck._CURSOR_FILE
    ck._CHAT_EVENTS_DIR = str(tmp_path / ".reyn" / "events" / "agents")
    ck._CURSOR_FILE = str(cursor_path)
    try:
        result = resolve_chat_scan_context({"data": {}})
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir
        ck._CURSOR_FILE = orig_cursor

    assert result["since"] == cursor_ts
    assert result["cursor_exists"] is True
    assert result["cursor_value"] == cursor_ts


def test_resolve_chat_scan_context_output_under_threshold(tmp_path, monkeypatch):
    """Tier 2: resolve_chat_scan_context output JSON < ARTIFACT_REF_THRESHOLD (8KB)."""
    from reyn.core.context_builder import ARTIFACT_REF_THRESHOLD

    # Create 120 dummy chat JSONL files
    agents_root = tmp_path / ".reyn" / "events" / "agents"
    for i in range(120):
        chat_dir = agents_root / f"agent_{i:03d}" / "chat" / "2026-06"
        chat_dir.mkdir(parents=True)
        (chat_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    orig_cursor = ck._CURSOR_FILE
    ck._CHAT_EVENTS_DIR = str(agents_root)
    ck._CURSOR_FILE = str(tmp_path / ".reyn" / "index" / "chat_cursor")
    try:
        result = resolve_chat_scan_context({"data": {"mode": "append"}})
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir
        ck._CURSOR_FILE = orig_cursor

    import json as _json
    serialized = _json.dumps(result, ensure_ascii=False)
    assert len(serialized) < ARTIFACT_REF_THRESHOLD, (
        f"resolve_chat_scan_context output exceeds ARTIFACT_REF_THRESHOLD "
        f"({ARTIFACT_REF_THRESHOLD} bytes): {len(serialized)} bytes."
    )
    # Must return count, not file list
    assert "chat_files_count" in result
    assert "chat_files" not in result


# ── Tier 2: postprocessor entry points ───────────────────────────────────────


def test_run_collect_chat_chunks_embeds_and_indexes(tmp_path, monkeypatch):
    """Tier 2: run_collect_chat_chunks embeds chat turns into the 'chat' index source."""
    agents_root = tmp_path / ".reyn" / "events" / "agents"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c001", user_text="test query"),
    )

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    ck._CHAT_EVENTS_DIR = str(agents_root)
    monkeypatch.chdir(str(tmp_path))
    try:
        result = run_collect_chat_chunks({"data": {"since": "1970-01-01T00:00:00Z"}})
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir

    assert result["chunk_count"] == 1, (
        f"Expected 1 chunk embedded, got {result['chunk_count']}"
    )
    assert result["embedded"] == 1

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

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    ck._CHAT_EVENTS_DIR = str(agents_root)
    monkeypatch.chdir(str(tmp_path))
    try:
        first = run_collect_chat_chunks({"data": {"since": "1970-01-01T00:00:00Z"}})
        assert first["embedded"] == 1

        second = run_collect_chat_chunks({"data": {"since": "1970-01-01T00:00:00Z"}})
        assert second["embedded"] == 0
        assert second["skipped_embed"] == 1
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir


def test_run_advance_chat_cursor_writes_correct_value(tmp_path, monkeypatch):
    """Tier 2: run_advance_chat_cursor advances .reyn/index/chat_cursor from chat_chunk_stats."""
    monkeypatch.chdir(str(tmp_path))
    (tmp_path / ".reyn" / "index").mkdir(parents=True)

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_cursor = ck._CURSOR_FILE
    ck._CURSOR_FILE = ".reyn/index/chat_cursor"
    try:
        artifact = {
            "data": {
                "chat_chunk_stats": {
                    "chunk_count": 3,
                    "skipped_turns": 0,
                    "max_turn_ts": "2026-06-15T10:00:00Z",
                }
            }
        }
        result = run_advance_chat_cursor(artifact)
    finally:
        ck._CURSOR_FILE = orig_cursor

    assert result["new_cursor"] == "2026-06-15T10:00:00Z"
    assert result["indexed_turns"] == 3
    assert result["sources_updated"] == ["chat"]
    assert read_chat_cursor(".reyn/index/chat_cursor") == "2026-06-15T10:00:00Z"


def test_run_collect_and_cursor_end_to_end(tmp_path, monkeypatch):
    """Tier 2: run_collect_chat_chunks + run_advance_chat_cursor end-to-end —
    cursor tracks the max turn_ts, and a second run using the cursor skips
    turns older than the cursor value.

    This is the key incremental indexing invariant for index_chat:
    a second invocation with since=<new_cursor> yields embedded=0 for
    the same turns already indexed in the first pass.
    """
    agents_root = tmp_path / ".reyn" / "events" / "agents"
    ts = "2026-06-15T08:00:00Z"
    _write_chat_jsonl(
        agents_root, "beta", "s.jsonl",
        _chat_events(chain_id="c001", turn_ts=ts),
    )

    import reyn.stdlib.skills.index_chat.chunkers as ck
    orig_dir = ck._CHAT_EVENTS_DIR
    orig_cursor = ck._CURSOR_FILE
    ck._CHAT_EVENTS_DIR = str(agents_root)
    ck._CURSOR_FILE = str(tmp_path / ".reyn" / "index" / "chat_cursor")
    monkeypatch.chdir(str(tmp_path))
    try:
        # First pass: embed 1 turn, advance cursor to ts
        stats = run_collect_chat_chunks({"data": {"since": "1970-01-01T00:00:00Z"}})
        assert stats["embedded"] == 1
        assert stats["max_turn_ts"] == ts

        artifact = {"data": {"chat_chunk_stats": stats}}
        cursor_result = run_advance_chat_cursor(artifact)
        assert cursor_result["new_cursor"] == ts

        # Second pass: since=ts → same turn passes (>= inclusive), but
        # content_hash dedup skips the re-embed
        stats2 = run_collect_chat_chunks({"data": {"since": ts}})
        assert stats2["embedded"] == 0, (
            "Expected 0 re-embeds on second pass: content_hash dedup must skip"
        )
        assert stats2["skipped_embed"] == 1
    finally:
        ck._CHAT_EVENTS_DIR = orig_dir
        ck._CURSOR_FILE = orig_cursor
