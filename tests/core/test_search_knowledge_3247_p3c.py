"""FP-0066 P3c (#3247 "P3 設計 firm" §2/§3/§5/§6) — the ``search_knowledge``
surface: the final P3 piece that lights up the knowledge-RAG feature for the
LLM (skill / memory / repo_doc / repo_src knowledge, previously write-only
after P3a/P3b).

Covers:
  1. §5 contract end-to-end: real memory + skill ingest (via the P3a
     producers) -> ``search_knowledge`` returns kind-native rows
     (``{kind, id, title, description}``).
  2. ★ §G1 chunk->entity aggregation: (a) the pure ``_aggregate_entities``
     function groups by ``(kind, id)``, keeps the max score, orders
     descending; (b) a real multi-chunk entity (two chunk rows sharing one
     ``source_path`` — the shape a future code-aware chunker would produce)
     collapses to exactly ONE row at the higher-scoring chunk's rank.
  3. #1822 classification: ``search_knowledge.returns_external_content is
     True`` (pinned exhaustively in ``test_returns_external_content_
     flagset_1822.py``; this file adds a direct, narrower check).
  4. visibility gate (firm §6, shared with search_actions):
     ``_enumerate_category("knowledge", ctx)`` is empty when embedding is
     not configured / ``rs`` is None, and returns ``search_knowledge``
     when it is.
  5. completeness (search_await contract): a dirty knowledge source is
     healed (re-built) before ``search_knowledge`` serves results.

No mocks — real ``SqliteIndexBackend``, real ``IndexCoordinator``, real
``SourceManifest``, real ``Workspace``/``OpContext``/``ToolContext``; a
plain fake embedding provider (same established convention as
``tests/test_fp0066_p3a_knowledge_ingest.py`` / ``test_index_coordinator_
3247_p2d.py``) stands in for the litellm boundary via the
``reyn.core.op_runtime.embed.get_provider`` monkeypatch seam.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index import SqliteIndexBackend
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.coordinator import get_index_coordinator
from reyn.data.index.knowledge_ingest import (
    KNOWLEDGE_MEMORY_SOURCE_ID,
    KNOWLEDGE_SKILL_SOURCE_ID,
    sync_memory_ingest,
    sync_skill_ingest,
)
from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.knowledge import SEARCH_KNOWLEDGE, _aggregate_entities
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _enumerate_category


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FixedVectorProvider:
    """Deterministic, per-text CONTROLLED vectors (not hash-derived) — needed
    so the aggregation test can force a known cosine-similarity ordering
    between two chunks of the SAME entity. Falls back to a zero-ish default
    vector for any text not explicitly mapped (memory/skill ingest embeds
    several texts we don't need to control precisely for the basic e2e
    contract test)."""

    def __init__(self, mapping: dict[str, list[float]], *, default: list[float]) -> None:
        self._mapping = mapping
        self._default = default

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        vectors = [self._mapping.get(t, list(self._default)) for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def _op_ctx_for(
    provider: Any, monkeypatch: pytest.MonkeyPatch, workspace: Workspace, events: EventLog,
) -> OpContext:
    import reyn.core.op_runtime.embed as _embed_mod

    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    return OpContext(workspace=workspace, events=events, permission_decl=PermissionDecl())


def _search_ctx(op_ctx: OpContext, provider: Any, workspace: Workspace, events: EventLog) -> ToolContext:
    rs = RouterCallerState(
        embedding_provider=provider,
        embedding_model_class="standard",
        op_context_factory=lambda: op_ctx,
    )
    return ToolContext(
        events=events, permission_resolver=None, workspace=workspace,
        caller_kind="router", router_state=rs,
    )


# ── 3. #1822 classification (direct, narrow check) ─────────────────────────


def test_returns_external_content_is_true() -> None:
    """Tier 1: search_knowledge is a discovery tool re-surfacing operator/
    user-authored content — flagged external (firm §2), the symmetric
    opposite of load_skill (_NOT_EXTERNAL, activation)."""
    assert SEARCH_KNOWLEDGE.returns_external_content is True


# ── 2a. §G1 aggregation — pure function ─────────────────────────────────────


def test_aggregate_entities_dedups_by_kind_id_keeps_max_score_orders_desc() -> None:
    """Tier 1: _aggregate_entities groups chunk-level rows by (kind, id),
    keeps the MAX-score row per entity, and orders the result by descending
    score — the §G1 contract in isolation from the query/backend machinery."""
    raw = [
        {"kind": "memory", "id": "shared/a.md", "title": "a", "description": "chunk1", "score": 0.2},
        {"kind": "memory", "id": "shared/a.md", "title": "a", "description": "chunk2-best", "score": 0.9},
        {"kind": "skill", "id": "alpha", "title": "alpha", "description": "skill chunk", "score": 0.5},
        {"kind": "memory", "id": "shared/b.md", "title": "b", "description": "b chunk", "score": 0.1},
    ]
    out = _aggregate_entities(raw)

    # One row per entity (2 memory entities collapsed to 2, not 3 raw rows).
    keys = [(r["kind"], r["id"]) for r in out]
    assert len(keys) == len(set(keys)) == 3

    # The duplicated entity kept its MAX-score chunk's description.
    a_row = next(r for r in out if r["id"] == "shared/a.md")
    assert a_row["description"] == "chunk2-best"

    # Ordered by descending score.
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(0.9)


def test_aggregate_entities_empty_input_is_empty_output() -> None:
    """Tier 1: vacuity — no chunk hits means no entities, not a crash."""
    assert _aggregate_entities([]) == []


# ── 4. visibility gate (shared with search_actions) ─────────────────────────


def test_enumerate_knowledge_category_hidden_without_router_state() -> None:
    """Tier 2: no router_state -> knowledge category hidden (degrade, not crash)."""
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None,
    )
    assert _enumerate_category("knowledge", ctx) == []


def test_enumerate_knowledge_category_hidden_when_embedding_not_configured() -> None:
    """Tier 2: router_state present but no embedding provider/model_class
    (embedding.enabled=false) -> knowledge category hidden, mirroring
    search_actions's own D14 gate (firm §6 set-sharing: same
    is_search_available predicate)."""
    rs = RouterCallerState(embedding_provider=None, embedding_model_class=None)
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=rs,
    )
    assert _enumerate_category("knowledge", ctx) == []


def test_enumerate_knowledge_category_visible_when_embedding_configured() -> None:
    """Tier 2: embedding_provider + embedding_model_class present (embedding.
    enabled=true) -> search_knowledge enumerated."""
    rs = RouterCallerState(embedding_provider=object(), embedding_model_class="standard")
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=rs,
    )
    entries = _enumerate_category("knowledge", ctx)
    assert [e["action_name"] for e in entries] == ["search_knowledge"]


# ── 1. §5 contract end-to-end (real memory + skill ingest) ─────────────────


def test_search_knowledge_returns_kind_native_rows_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: real memory + skill ingest (P3a producers), then
    search_knowledge returns one entity-level row per source with the
    kind-native id (§5) — no abstract handle, no unified load verb."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    provider = _FixedVectorProvider({}, default=[0.5, 0.5, 0.5, 0.5])
    op_ctx = _op_ctx_for(provider, monkeypatch, ws, events)
    coordinator = get_index_coordinator(tmp_path)

    # Real memory entry via the P3a producer.
    _run(sync_memory_ingest(coordinator, tmp_path, op_ctx))
    # sync_memory_ingest ingests whatever is on disk; write one entry first.
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "note1.md").write_text("about widgets and gadgets", encoding="utf-8")
    _run(sync_memory_ingest(coordinator, tmp_path, op_ctx))

    # Real skill entry via the P3a producer.
    skill_dir = tmp_path / "skills" / "widget-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# Widget Skill\nHandles widgets.", encoding="utf-8")
    raw_skills = {
        "entries": {
            "widget-skill": {
                "path": str(skill_dir), "description": "Handles widget-related tasks",
            },
        },
    }
    _run(sync_skill_ingest(coordinator, raw_skills, op_ctx))

    manifest = get_source_manifest(tmp_path)
    assert _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID)).state == "clean"
    assert _run(manifest.get(KNOWLEDGE_SKILL_SOURCE_ID)).state == "clean"

    ctx = _search_ctx(op_ctx, provider, ws, events)
    result = _run(SEARCH_KNOWLEDGE.handler({"query": "widgets"}, ctx))

    assert "error" not in result
    items = result["items"]
    assert result["total"] == len(items)
    kinds = {item["kind"] for item in items}
    assert kinds <= {"memory", "skill", "repo_doc", "repo_src"}
    assert {"memory", "skill"} <= kinds, f"expected both memory + skill hits, got {items}"

    memory_row = next(i for i in items if i["kind"] == "memory")
    assert memory_row["id"] == "shared/note1.md", "id must be kind-native (memory doc path)"
    assert memory_row["title"] == "note1"

    skill_row = next(i for i in items if i["kind"] == "skill")
    assert skill_row["id"] == "widget-skill", "id must be kind-native (skill name, no abstract handle)"
    assert skill_row["title"] == "widget-skill"

    # §5: no abstract handle field, no unified activation verb in the shape.
    for item in items:
        assert set(item.keys()) == {"kind", "id", "title", "description"}


def test_search_knowledge_missing_query_returns_error_envelope() -> None:
    """Tier 1: missing/empty query -> a §D12-style error envelope, not a crash."""
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None,
    )
    result = _run(SEARCH_KNOWLEDGE.handler({}, ctx))
    assert "error" in result


def test_search_knowledge_degrades_empty_without_embedding_configured(tmp_path: Path) -> None:
    """Tier 2: router_state present but embedding not configured -> empty
    result, not a crash (mirrors search_actions's own degrade)."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    rs = RouterCallerState(embedding_provider=None, embedding_model_class=None)
    ctx = ToolContext(
        events=events, permission_resolver=None, workspace=ws,
        caller_kind="router", router_state=rs,
    )
    result = _run(SEARCH_KNOWLEDGE.handler({"query": "anything"}, ctx))
    assert result == {"items": [], "total": 0}


# ── 2b. §G1 aggregation — real multi-chunk entity through the full stack ───


def test_search_knowledge_aggregates_multichunk_entity_to_one_row_at_max_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: two chunk rows sharing ONE entity (source_path) — the shape
    a future code-aware chunker (firm §12) would produce — collapse to
    exactly ONE search_knowledge row, at the higher-scoring chunk (§G1).

    Writes both chunks DIRECTLY to the backend (bypassing embed — the write
    side needs precise, opposed vectors to force a deterministic cosine
    ranking) and marks the source "clean" so search_await is a no-op; only
    the QUERY embed goes through the (monkeypatched) provider, mapped to
    the vector that exactly matches the "best" chunk.
    """
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    query_vector = [1.0, 0.0, 0.0, 0.0]
    provider = _FixedVectorProvider({"widgets": query_vector}, default=[0.0, 0.0, 0.0, 1.0])
    op_ctx = _op_ctx_for(provider, monkeypatch, ws, events)

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    records = [
        ChunkRecord(
            text="best chunk (exact match)",
            vector=[1.0, 0.0, 0.0, 0.0],  # cosine 1.0 with the query
            metadata={
                "source_path": "shared/dup.md", "source_type": "memory",
                "content_hash": "dup-chunk-a", "embedding_model": "standard",
                "chunk_index": 0, "size_tokens": 0, "parent_context": None,
                "extra": {"layer": "shared", "slug": "dup"},
            },
            score=None,
        ),
        ChunkRecord(
            text="weak chunk (orthogonal)",
            vector=[0.0, 1.0, 0.0, 0.0],  # cosine 0.0 with the query
            metadata={
                "source_path": "shared/dup.md", "source_type": "memory",
                "content_hash": "dup-chunk-b", "embedding_model": "standard",
                "chunk_index": 1, "size_tokens": 0, "parent_context": None,
                "extra": {"layer": "shared", "slug": "dup"},
            },
            score=None,
        ),
    ]
    _run(backend.write(KNOWLEDGE_MEMORY_SOURCE_ID, records, mode="replace"))

    manifest = get_source_manifest(tmp_path)
    _run(manifest.upsert(SourceEntry(
        name=KNOWLEDGE_MEMORY_SOURCE_ID, description="", path="",
        kind="dynamic", state="clean", chunk_count=2,
    )))

    ctx = _search_ctx(op_ctx, provider, ws, events)
    result = _run(SEARCH_KNOWLEDGE.handler({"query": "widgets"}, ctx))

    memory_items = [i for i in result["items"] if i["kind"] == "memory"]
    # Behavioral pin (not a size pin): the ENTIRE memory slice must be
    # exactly this one row — a two-chunk entity collapsed to its
    # HIGHER-scoring chunk (§G1), not a size assertion in isolation.
    assert memory_items == [
        {
            "kind": "memory", "id": "shared/dup.md", "title": "dup",
            "description": "best chunk (exact match)",
        },
    ], f"one entity hit by 2 chunks must collapse to exactly its best chunk, got {memory_items}"


# ── 5. completeness — search_await heals a dirty source before serving ─────


def test_search_knowledge_heals_dirty_source_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: a dirty knowledge source (a prior sync-in-op provider
    failure, per §G2) is healed — re-built — by search_await BEFORE
    search_knowledge serves results, the completeness guarantee ("best-
    effort search is a bug") the Coordinator's search_await contract
    exists to close."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    provider = _FixedVectorProvider({}, default=[0.5, 0.5, 0.5, 0.5])
    op_ctx = _op_ctx_for(provider, monkeypatch, ws, events)
    coordinator = get_index_coordinator(tmp_path)

    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "heal-me.md").write_text("content to heal", encoding="utf-8")

    # First ingest succeeds (registers the builder + writes clean).
    _run(sync_memory_ingest(coordinator, tmp_path, op_ctx))
    manifest = get_source_manifest(tmp_path)
    assert _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID)).state == "clean"

    # Simulate a later provider-failure leaving the source dirty (§G2) —
    # the builder stays registered on `coordinator` (same process), so
    # search_await CAN heal it.
    _run(coordinator.mark_dirty(KNOWLEDGE_MEMORY_SOURCE_ID, reason="simulated_provider_error"))
    assert _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID)).state == "dirty"

    ctx = _search_ctx(op_ctx, provider, ws, events)
    result = _run(SEARCH_KNOWLEDGE.handler({"query": "heal"}, ctx))

    assert "error" not in result
    healed = _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert healed.state == "clean", "search_knowledge must heal the dirty source via search_await"
