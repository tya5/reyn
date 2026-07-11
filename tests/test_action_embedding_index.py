"""Tier 2: FP-0034 Phase 2 step 1 — ActionEmbeddingIndex.

Verifies the in-memory action embedding index that powers
``search_actions``.  Uses a plain ``FakeEmbeddingProvider`` (= no
mocks, no LLMReplay) so the tests pin the index contract independently
of any real embedding backend.

Coverage:
  - is_ready() lifecycle (= False → True after build)
  - catalog_hash idempotence (= same hash = no-op rebuild)
  - hash sensitivity to qualified_name set changes
  - query top-K ranking via cosine similarity
  - empty / whitespace query degrades to []
  - not-ready query degrades to []
  - top_k <= 0 degrades to []
  - mismatched provider vector count refuses the partial build
  - empty catalog handled cleanly (empty hash recorded)

No mocks. The FakeEmbeddingProvider is a plain async class
implementing the EmbeddingProvider Protocol's ``embed`` method.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.action_index import (
    ActionEmbeddingIndex,
    compute_catalog_hash,
)


def _ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch) -> OpContext:
    """Build a real OpContext whose `embed` op resolves to ``provider``.

    FP-0057 #2856 Part A: ``ActionEmbeddingIndex.build()``/``query()`` now
    route the embed call through ``execute_op(EmbedIROp(...), ctx)`` (the
    shared `embed` op) instead of calling a caller-held provider directly —
    tests monkeypatch the op-runtime module's ``get_provider`` (the
    established convention, see ``tests/test_op_embed.py``) instead of
    passing the fake provider as a positional argument.
    """
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())

# ── Fake EmbeddingProvider ────────────────────────────────────────────────


class _FakeEmbeddingProvider:
    """In-test EmbeddingProvider — deterministic canned vectors.

    Encodes the text as a 4-dimension vector where each component
    derives from a hash of the input + a position-specific seed.
    Gives distinct vectors per distinct text so cosine similarity
    rankings are non-degenerate.
    """
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append((tuple(texts), model))
        vectors: list[list[float]] = []
        for t in texts:
            # 4-dim vector seeded by per-component hash buckets so
            # distinct strings produce distinct vectors.
            v = [
                float((hash((t, i)) % 1000) / 1000.0) for i in range(4)
            ]
            vectors.append(v)
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


class _DegenerateFakeProvider:
    """Returns the WRONG number of vectors — for partial-build refusal test."""
    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        return {
            "vectors": [[1.0, 0.0]],  # always 1 vector regardless of input
            "model": model,
            "total_tokens": 1,
        }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 1. is_ready() lifecycle ───────────────────────────────────────────────


def test_initial_state_not_ready() -> None:
    """Tier 2: fresh index reports not ready."""
    idx = ActionEmbeddingIndex()
    assert idx.is_ready() is False
    assert idx.size() == 0
    assert idx.catalog_hash() is None


def test_build_then_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: build() transitions index to ready."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        ctx,
        "standard",
    ))
    assert idx.is_ready() is True
    assert idx.size() == 1
    assert idx.catalog_hash() is not None


# ── 2. catalog_hash semantics ─────────────────────────────────────────────


def test_catalog_hash_stable_to_order() -> None:
    """Tier 2: catalog hash is order-independent (sorted qualified_names)."""
    a = compute_catalog_hash([
        {"qualified_name": "skill__foo"},
        {"qualified_name": "skill__bar"},
    ])
    b = compute_catalog_hash([
        {"qualified_name": "skill__bar"},
        {"qualified_name": "skill__foo"},
    ])
    assert a == b


def test_catalog_hash_changes_when_names_change() -> None:
    """Tier 2: adding a qualified_name changes the hash."""
    a = compute_catalog_hash([{"qualified_name": "skill__foo"}])
    b = compute_catalog_hash([
        {"qualified_name": "skill__foo"},
        {"qualified_name": "skill__bar"},
    ])
    assert a != b


def test_idempotent_rebuild_with_same_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: second build() with same catalog is a no-op (no re-embed)."""
    items = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]
    provider = _FakeEmbeddingProvider()
    ctx = _ctx_for(provider, monkeypatch)
    idx = ActionEmbeddingIndex()
    _run(idx.build(items, ctx, "standard"))
    first_call_count = len(provider.calls)
    _run(idx.build(items, ctx, "standard"))
    assert len(provider.calls) == first_call_count, (
        "Second build with identical catalog must not re-embed; "
        f"provider.calls grew from {first_call_count} to {len(provider.calls)}"
    )


def test_rebuild_when_catalog_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: changing the qualified_name set triggers a fresh build."""
    provider = _FakeEmbeddingProvider()
    ctx = _ctx_for(provider, monkeypatch)
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        ctx, "standard",
    ))
    first_call_count = len(provider.calls)
    _run(idx.build(
        [
            {"qualified_name": "skill__a", "short_description": "A"},
            {"qualified_name": "skill__b", "short_description": "B"},
        ],
        ctx, "standard",
    ))
    assert len(provider.calls) > first_call_count
    assert idx.size() == 2


# ── 3. query() — top-K ranking ────────────────────────────────────────────


def test_query_returns_top_k_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: query returns top_k results sorted by score descending."""
    items = [
        {"qualified_name": f"skill__item_{i}", "short_description": f"Item {i}"}
        for i in range(5)
    ]
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(items, ctx, "standard"))
    results = _run(idx.query("query for item 0", ctx, "standard", top_k=3))
    (r0, r1, r2) = results
    # Scores must be monotonically non-increasing.
    scores = [r["score"] for r in (r0, r1, r2)]
    assert scores == sorted(scores, reverse=True)
    # Each result has the original fields + score.
    for r in (r0, r1, r2):
        assert "qualified_name" in r
        assert "short_description" in r
        assert "score" in r
        assert -1.0 <= r["score"] <= 1.0


def test_query_top_k_larger_than_catalog_returns_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: top_k > catalog size returns the full catalog."""
    items = [
        {"qualified_name": "skill__only", "short_description": "Only one"},
    ]
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(items, ctx, "standard"))
    results = _run(idx.query("anything", ctx, "standard", top_k=10))
    (only,) = results


# ── 4. Graceful degradation ───────────────────────────────────────────────


def test_query_not_ready_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: querying before build() returns []."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    assert _run(idx.query("anything", ctx, "standard")) == []


def test_query_empty_string_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: empty query returns [] (skip wasteful embedding call)."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        ctx, "standard",
    ))
    assert _run(idx.query("", ctx, "standard")) == []
    assert _run(idx.query("   ", ctx, "standard")) == []


def test_query_zero_top_k_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: top_k <= 0 returns []."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        ctx, "standard",
    ))
    assert _run(idx.query("q", ctx, "standard", top_k=0)) == []
    assert _run(idx.query("q", ctx, "standard", top_k=-1)) == []


# ── 5. Edge cases ─────────────────────────────────────────────────────────


def test_empty_catalog_records_hash_no_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: build() with empty items still records the empty hash."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build([], ctx, "standard"))
    assert idx.is_ready() is True
    assert idx.size() == 0
    assert idx.catalog_hash() is not None


def test_items_without_qualified_name_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: items missing qualified_name are silently skipped."""
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(
        [
            {"qualified_name": "skill__valid", "short_description": "Valid"},
            {"short_description": "No qualified_name field"},  # dropped
            {"qualified_name": "", "short_description": "Empty name"},  # dropped
        ],
        ctx,
        "standard",
    ))
    assert idx.size() == 1


def test_mismatched_vector_count_refuses_partial_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: provider returning wrong vector count → RuntimeError, no state.

    Refuses partial state so we don't end up with a corrupt half-populated
    index.  The catalog hash stays None so the next build retries.
    """
    idx = ActionEmbeddingIndex()
    ctx = _ctx_for(_DegenerateFakeProvider(), monkeypatch)
    with pytest.raises(RuntimeError, match="refusing partial build"):
        _run(idx.build(
            [
                {"qualified_name": "skill__a"},
                {"qualified_name": "skill__b"},  # 2 items but provider returns 1
            ],
            ctx,
            "standard",
        ))
    assert idx.is_ready() is False
    assert idx.size() == 0
    assert idx.catalog_hash() is None


# ── 6. Unified-backend persistence (FP-0057 Phase 0) ─────────────────────
#
# ActionEmbeddingIndex now delegates storage to the pluggable IndexBackend
# (the same SqliteIndexBackend doc-RAG uses) instead of a private SQLite
# schema, so these tests assert through the PUBLIC surface (is_ready() /
# size() / catalog_hash() / db_path existence) rather than reaching into
# table names/columns — reaching into the on-disk schema would be a
# private-storage-format pin the testing policy forbids.


def test_workspace_root_build_creates_db_file(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: build() with a workspace_root creates the unified index.db."""
    items = [{"qualified_name": "skill__a", "short_description": "A"}]
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(items, ctx, "standard"))

    assert idx.db_path is not None and idx.db_path.exists(), (
        "index.db must be created after build()"
    )
    assert idx.size() == 1
    assert idx.catalog_hash() is not None


def test_unified_path_replaces_old_action_index_dir(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: FP-0057 Phase 0 clean-break — storage lands under the unified
    ``.reyn/cache/index/<source>/`` convention, NOT the old private
    ``.reyn/cache/action_index/`` directory (which is no longer read or
    written; regenerable cache, no migration needed)."""
    items = [{"qualified_name": "skill__a", "short_description": "A"}]
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx.build(items, ctx, "standard"))

    old_dir = tmp_path / ".reyn" / "cache" / "action_index"
    assert not old_dir.exists(), (
        "the old pre-consolidation action_index cache dir must not be "
        "written by the unified adapter"
    )
    assert idx.db_path is not None
    assert idx.db_path.parent == tmp_path / ".reyn" / "cache" / "index" / "actions"


def test_workspace_root_loads_from_disk_skips_embed(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: second index with the same workspace_root loads from disk,
    skips embed.

    Simulates a process restart: a fresh ActionEmbeddingIndex is created
    (empty in-memory state) and build() is called with the same catalog.
    The disk cache hit must prevent any embed call.
    """
    items = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]

    # First process — build + persist
    idx1 = ActionEmbeddingIndex(workspace_root=tmp_path)
    provider1 = _FakeEmbeddingProvider()
    ctx1 = _ctx_for(provider1, monkeypatch)
    _run(idx1.build(items, ctx1, "standard"))
    (only_call,) = provider1.calls

    # Second process (simulated) — fresh index, same catalog
    idx2 = ActionEmbeddingIndex(workspace_root=tmp_path)
    provider2 = _FakeEmbeddingProvider()
    ctx2 = _ctx_for(provider2, monkeypatch)
    _run(idx2.build(items, ctx2, "standard"))

    assert not provider2.calls, (
        "build() must load from disk and skip the embed call on cache hit"
    )
    assert idx2.is_ready() is True
    assert idx2.size() == 2
    assert idx2.catalog_hash() == idx1.catalog_hash()


def test_workspace_root_rebuilds_on_stale_disk_hash(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: changed catalog triggers re-embed and overwrites disk hash."""
    items_v1 = [{"qualified_name": "skill__a", "short_description": "A"}]
    items_v2 = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]

    idx1 = ActionEmbeddingIndex(workspace_root=tmp_path)
    ctx1 = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    _run(idx1.build(items_v1, ctx1, "standard"))
    hash_v1 = idx1.catalog_hash()

    # Fresh index — different catalog → must re-embed, not load v1 from disk
    idx2 = ActionEmbeddingIndex(workspace_root=tmp_path)
    provider2 = _FakeEmbeddingProvider()
    ctx2 = _ctx_for(provider2, monkeypatch)
    _run(idx2.build(items_v2, ctx2, "standard"))

    (only_call,) = provider2.calls  # stale disk hash must trigger re-embed
    assert idx2.size() == 2
    assert idx2.catalog_hash() != hash_v1

    # A third fresh instance against the same workspace_root now observes
    # the new hash (= the disk write-through actually took).
    idx3 = ActionEmbeddingIndex(workspace_root=tmp_path)
    provider3 = _FakeEmbeddingProvider()
    ctx3 = _ctx_for(provider3, monkeypatch)
    _run(idx3.build(items_v2, ctx3, "standard"))
    assert not provider3.calls
    assert idx3.catalog_hash() == idx2.catalog_hash()


# ── 7. FP-0057 #2856 Part A — redaction-bypass closed ────────────────────
#
# Pre-#2856, build()/query() called `provider.embed(...)` PROVIDER-DIRECT,
# bypassing the shared `embed` op's PRE-embed redaction-egress scan
# (co-vet #3 in core/op_runtime/embed.py). Routing through
# execute_op(EmbedIROp(...), ctx) (Part A) means a secret-shaped string
# anywhere in the catalog (e.g. a tool's short_description) is redacted
# BEFORE it reaches the (fake, in-test) embedding provider — the same
# egress boundary an external embedding API would sit behind in production.


def test_build_redacts_secret_in_short_description_before_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a secret-shaped short_description is redacted at the embed
    egress BEFORE the (fake) provider ever sees it — the tool-use
    provider-direct redaction bypass this Part A closes."""
    provider = _FakeEmbeddingProvider()
    ctx = _ctx_for(provider, monkeypatch)
    idx = ActionEmbeddingIndex()
    secret_desc = 'api_key = "abcdefghijklmnopqrstuvwxyz123456"'
    _run(idx.build(
        [{"qualified_name": "skill__leaky", "short_description": secret_desc}],
        ctx,
        "standard",
    ))
    ((embedded_texts, _model),) = provider.calls
    (embedded_text,) = embedded_texts
    assert "abcdefghijklmnopqrstuvwxyz123456" not in embedded_text, (
        "the raw secret must never reach the embedding provider"
    )
    assert "REDACTED" in embedded_text
    # The seam firing is observable (P6 audit-event trace) on the ctx used
    # for the build.
    assert any(e.type == "embed_secret_redacted" for e in ctx.events.all())
