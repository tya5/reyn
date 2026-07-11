"""Tier 2: index_update op handler OS invariants (FP-0057 Phase 2a).

Incremental / delta-reconcile ingestion — NO full-rebuild mode. Tests use a
real FakeEmbeddingProvider (monkeypatched into both `op_runtime.embed`'s and
`op_runtime.index_update`'s module-level `get_provider`, mirroring
`tests/test_op_embed.py`'s pattern — the index_update handler dispatches the
actual embed call through the shared `embed` op via `execute_op`, and
separately resolves a provider for the cost-estimate) and a real
SqliteIndexBackend + SourceManifest for end-to-end dispatch. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import IndexUpdateIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

class FakeEmbeddingProvider:
    """Deterministic real EmbeddingProvider: one fixed-shape vector per text,
    records every (texts, model) embed call for assertions."""

    def __init__(self) -> None:
        self._batch_size = 10
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        self.calls.append((tuple(texts), model))
        vectors = [[float(len(t)), 0.0, 1.0] for t in texts]
        return EmbedBatchResult(vectors=vectors, model=model, total_tokens=len(texts))

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t.split()) for t in texts)

    def get_dimension(self, model: str) -> int:
        return 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


def _wire_fake_provider(monkeypatch: pytest.MonkeyPatch, fake: FakeEmbeddingProvider) -> None:
    """index_update dispatches the actual embed through the shared `embed` op
    (op_runtime.embed's own get_provider) and separately resolves a provider
    for the pre-embed cost estimate (op_runtime.index_update's get_provider)
    — both must point at the SAME fake so call-recording is consistent."""
    import reyn.core.op_runtime.embed as _embed_mod
    import reyn.core.op_runtime.index_update as _iu_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)
    monkeypatch.setattr(_iu_mod, "get_provider", lambda *a, **kw: fake)


def _chunk(text: str, content_hash: str, source_path: str) -> dict:
    return {
        "text": text,
        "metadata": {"content_hash": content_hash, "source_path": source_path},
    }


# ---------------------------------------------------------------------------
# Tier 1: registration
# ---------------------------------------------------------------------------

def test_index_update_registered_in_op_kind_model_map() -> None:
    """Tier 1: `index_update` is a first-class Control IR op kind (hard-rule
    sync: OP_KIND_MODEL_MAP <-> control-ir.md, #1983)."""
    from reyn.core.op_runtime import available_kinds
    from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, IndexUpdateIROp

    assert "index_update" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["index_update"] is IndexUpdateIROp
    assert "index_update" in ALL_OP_KINDS
    assert "index_update" in available_kinds()


def test_index_update_op_kind_has_a_contextual_gate_entry() -> None:
    """Tier 1: `index_update` is registered in the contextual-gate op-kind
    table (per-session capability narrowing), same shape as embed/index_query."""
    from reyn.core.op_runtime.contextual_gate import op_kind_tool_names

    names = op_kind_tool_names("index_update")
    assert "index_update" in names


# ---------------------------------------------------------------------------
# Tier 2: add / update / remove / skip reconciliation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_update_add_new_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: fresh chunks for a fresh source are all ADDED (embedded + inserted)."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = IndexUpdateIROp(
        kind="index_update",
        source="docs",
        chunks=[
            _chunk("chunk a", "ha", "a.md"),
            _chunk("chunk b", "hb", "b.md"),
        ],
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["added"] == 2
    assert result["updated"] == 0
    assert result["removed"] == 0
    assert result["skipped"] == 0
    assert result["chunk_count"] == 2

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    hashes = await backend.existing_hashes("docs")
    assert hashes == {"ha", "hb"}


@pytest.mark.asyncio
async def test_index_update_skips_unchanged_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: re-supplying the SAME content_hash a second time is a no-op
    (no re-embed) — the pre-embed dedup key."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    first = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a", "ha", "a.md")],
    )
    await execute_op(first, ctx)
    embed_calls_after_first = len(fake.calls)

    second = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a", "ha", "a.md")],
    )
    result = await execute_op(second, ctx)

    assert result.get("status") != "error", result
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["removed"] == 0
    assert result["skipped"] == 1
    # unpack: no NEW embed call fired on the second (unchanged) call.
    assert len(fake.calls) == embed_calls_after_first


@pytest.mark.asyncio
async def test_index_update_updates_changed_content_under_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a NEW content_hash under an ALREADY-indexed source_path is an
    UPDATE (re-embedded + inserted; the path's stale hash is removed in the
    same pass — the reconciliation is content-addressed per path)."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    first = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a v1", "ha1", "a.md")],
    )
    await execute_op(first, ctx)

    # a.md's content changed -> new hash, same path, this call re-supplies a.md
    second = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a v2", "ha2", "a.md")],
    )
    result = await execute_op(second, ctx)

    assert result.get("status") != "error", result
    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["removed"] == 1  # the stale "ha1" hash
    assert result["skipped"] == 0

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    hashes = await backend.existing_hashes("docs")
    assert hashes == {"ha2"}


@pytest.mark.asyncio
async def test_index_update_removes_stale_chunk_under_resupplied_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a path with TWO indexed chunks, re-supplied with only ONE of
    them (no replacement chunk for the other) — the un-resupplied hash is
    REMOVED (pure deletion, not an add/update pair) while the resupplied one
    is SKIPPED (unchanged)."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    first = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[
            _chunk("a chunk 1", "ha1", "a.md"),
            _chunk("a chunk 2", "ha2", "a.md"),
        ],
    )
    await execute_op(first, ctx)

    # a.md now has only ONE chunk (ha2 dropped from the source file) —
    # this call re-supplies a.md (so its stale chunk IS reconciled) but
    # only lists ha1.
    second = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("a chunk 1", "ha1", "a.md")],
    )
    result = await execute_op(second, ctx)

    assert result.get("status") != "error", result
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["removed"] == 1  # ha2
    assert result["skipped"] == 1  # ha1 unchanged

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    hashes = await backend.existing_hashes("docs")
    assert hashes == {"ha1"}


@pytest.mark.asyncio
async def test_index_update_leaves_unmentioned_path_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: reconciliation is scoped to the source_paths THIS call
    supplies chunks for — a path never mentioned in a later call is left
    alone (a partial re-ingest of a few files never mass-deletes the rest
    of the source)."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    first = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[
            _chunk("chunk a", "ha", "a.md"),
            _chunk("chunk b", "hb", "b.md"),
        ],
    )
    await execute_op(first, ctx)

    # This call only re-supplies a.md — b.md is never mentioned.
    second = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a", "ha", "a.md")],
    )
    result = await execute_op(second, ctx)

    assert result.get("status") != "error", result
    assert result["removed"] == 0  # b.md's chunk survives — never mentioned
    assert result["skipped"] == 1  # a.md's chunk unchanged

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    hashes = await backend.existing_hashes("docs")
    assert hashes == {"ha", "hb"}


# ---------------------------------------------------------------------------
# co-vet #4 — cost-estimator wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_update_cost_warning_fires_over_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: co-vet #4 — a to-embed batch exceeding `embedding.
    cost_warn_threshold` surfaces a non-blocking `cost_warning` field +
    an `index_update_cost_warning` audit-event (closes the previously
    dead-code gap where `cost_warn_threshold` was parsed from config but no
    caller ever read it)."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    from reyn.config.embedding import EmbeddingConfig

    class _FakeReynConfig:
        embedding = EmbeddingConfig(cost_warn_threshold=1)  # 1 chunk triggers the warning

    # index_update resolves `load_config()` via a local `from reyn.config import
    # load_config` at call time — patching the package attribute is what the
    # local import actually looks up.
    import reyn.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_config", lambda *a, **kw: _FakeReynConfig())

    ctx = _make_ctx(tmp_path)
    op = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[
            _chunk("chunk a", "ha", "a.md"),
            _chunk("chunk b", "hb", "b.md"),
        ],
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["cost_warning"] is not None
    assert result["cost_warning"]["chunk_count"] == 2
    assert result["cost_warning"]["threshold"] == 1
    assert any(e.type == "index_update_cost_warning" for e in ctx.events.all())


@pytest.mark.asyncio
async def test_index_update_no_cost_warning_under_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a to-embed batch under the (default, large) threshold does
    NOT fire the cost warning."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a", "ha", "a.md")],
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["cost_warning"] is None
    assert not any(e.type == "index_update_cost_warning" for e in ctx.events.all())


# ---------------------------------------------------------------------------
# Source-model-bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_update_reuses_recorded_source_model_over_new_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a source's recorded embedding model (set on first ingestion)
    wins over a later call's `embedding_model` override — a source is one
    embedding space."""
    fake = FakeEmbeddingProvider()
    _wire_fake_provider(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    first = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk a", "ha", "a.md")],
        embedding_model="modelA",
    )
    await execute_op(first, ctx)

    second = IndexUpdateIROp(
        kind="index_update", source="docs",
        chunks=[_chunk("chunk c", "hc", "c.md")],
        embedding_model="modelB",  # ignored — docs already recorded modelA
    )
    result = await execute_op(second, ctx)

    assert result.get("status") != "error", result
    assert result["embedding_model"] == "modelA"
    models_used = {model for _texts, model in fake.calls}
    assert models_used == {"modelA"}
