"""FP-0066 P3-helper (#3247 firm §6) — the unified ``emit_wrapped_semantic_
search`` helper + the catalog-site complete-emit bug fix it exposes.

Context: ``semantic_search_started -> search_await -> query ->
semantic_search_complete`` was duplicated verbatim at the two live query
call sites (``RouterLoop.search_actions`` in ``router_loop.py`` ~L2904-2943,
``universal_catalog._handle_search_actions`` ~L974-985) — a third caller
(``search_knowledge``, P3c) would make it three, so the firm (#3247, P3
"設計 firm" §6) calls for extraction into ``reyn.data.index.coordinator.
emit_wrapped_semantic_search`` BEFORE that lands.

Covers:
  1. ★ The bug fix — pre-helper, the catalog call site had NO try/finally,
     so an ``index.query()`` failure emitted ``semantic_search_started``
     without its matching ``_complete`` (a dangling started-without-
     complete in the audit trail). This test drives that path through the
     REAL production handler (``SEARCH_ACTIONS.handler``) with a query
     that genuinely fails (a failing embedding provider swapped in for the
     query-time embed, mirroring ``test_index_coordinator_3247_p2d.py``'s
     established fake-provider convention) and asserts ``_complete`` FIRES
     (``results=0``) despite the failure, the error re-raises, and
     started/complete come in a matched pair.
  2. The helper is ``events``-None-tolerant and ``coordinator``-None-
     tolerant (the catalog site's pre-existing degrade when
     ``ctx.workspace`` is unset) without raising.
  3. The router_loop call site's pre-existing behavioral test
     (``test_router_loop_search_actions_wires_search_await_and_emits_audit``
     in ``test_index_coordinator_3247_p2d.py``) is UNCHANGED by this PR —
     not re-declared here; its continued green run is this PR's byte-
     identical-preservation proof for that site.

No mocks — real ``IndexCoordinator``, real ``ActionEmbeddingIndex``, a real
``EventLog``/``EventStore`` pair (audit-events read back from the actual
on-disk JSONL, not private state), a real ``OpContext``; a plain fake
embedding provider (same established convention as
``test_index_coordinator_3247_p2d.py``) stands in for the litellm boundary.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.coordinator import emit_wrapped_semantic_search, get_index_coordinator
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.action_index import ActionEmbeddingIndex
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import SEARCH_ACTIONS
from tests._support.events import settle


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _events_and_store(tmp_path: Path) -> tuple[EventLog, EventStore]:
    """A real EventLog subscribed to a real EventStore rooted under
    ``tmp_path / ".reyn" / "events"`` — audit-events are read back from the
    actual on-disk JSONL, per CLAUDE.md's real-instance testing policy."""
    store = EventStore(tmp_path / ".reyn" / "events")
    log = EventLog(subscribers=[store])
    return log, store


async def _read_back(log: EventLog, store: EventStore) -> list[dict]:
    # #4966: `store` is a SUBSCRIBER of `log` — dispatch to it is
    # asynchronous whenever a loop is running (#4961 C), so `store.flush()`
    # alone can race the still-in-flight delivery. Settle first.
    await settle(log)
    await store.flush()
    return [e.model_dump(mode="json") for e in store.iter_all()]


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text (no litellm call)."""

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


class _FailingEmbeddingProvider:
    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        raise RuntimeError("embedding API unreachable")


def _op_ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch, events: EventLog) -> OpContext:
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


# ── 1. ★ bug-fix proof — catalog site now guarantees complete-emit ────────


def test_catalog_search_actions_emits_complete_on_query_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: pre-``emit_wrapped_semantic_search``, ``universal_catalog.
    _handle_search_actions`` had NO try/finally around ``idx.query()`` — a
    query failure left ``semantic_search_started`` emitted with no matching
    ``semantic_search_complete`` (a real, previously-unproven leak). After
    the P3-helper extraction, the helper's try/finally guarantees
    ``_complete`` (``results=0``) fires even when the query fails, and the
    original error still re-raises to the caller (the handler itself does
    not swallow it — unchanged from before)."""
    log, store = _events_and_store(tmp_path)
    build_provider = _FakeEmbeddingProvider()
    build_ctx = _op_ctx_for(build_provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    items = [{"action_name": "skill__alpha", "short_description": "Alpha skill"}]
    _run(idx.build(items, build_ctx, "standard"))
    assert idx.is_ready() is True

    # Mark the coordinator's manifest "clean" so search_await is a no-op —
    # this test's claim is about the query-failure leak, not build/heal.
    coordinator = get_index_coordinator(tmp_path)
    from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
    manifest = get_source_manifest(tmp_path)
    _run(manifest.upsert(SourceEntry(
        name="actions", description="", path="", kind="static", state="clean",
    )))

    # The query-time embed call now fails (swap in a failing provider for
    # THIS OpContext) — idx.query() raises RuntimeError (per
    # ActionEmbeddingIndex._embed_via_op's documented raise-on-op-failure
    # contract), independent of the earlier successful build.
    query_provider = _FailingEmbeddingProvider()
    query_ctx = _op_ctx_for(query_provider, monkeypatch, log)

    ws = Workspace(events=log, base_dir=tmp_path)
    rs = RouterCallerState(
        action_embedding_index=idx,
        embedding_provider=query_provider,
        embedding_model_class="standard",
        op_context_factory=lambda: query_ctx,
    )
    ctx = ToolContext(
        events=log, permission_resolver=None, workspace=ws,
        caller_kind="router", router_state=rs,
    )

    async def _scenario() -> list[dict]:
        with pytest.raises(RuntimeError, match="embed op failed"):
            await SEARCH_ACTIONS.handler({"query": "alpha", "limit": 5}, ctx)
        return await _read_back(log, store)

    events = _run(_scenario())
    kinds = [e["type"] for e in events if e["type"].startswith("semantic_search_")]
    assert kinds == ["semantic_search_started", "semantic_search_complete"], (
        "started/complete must come as a MATCHED PAIR even on query "
        "failure — pre-helper, complete never fired here (the bug this "
        "extraction fixes)"
    )
    complete = next(e for e in events if e["type"] == "semantic_search_complete")
    assert complete["data"]["source_id"] == "actions"
    assert complete["data"]["results"] == 0, (
        "results must be 0 on a query failure, not omitted/stale"
    )
    del coordinator  # referenced only to force the manifest write above


# ── 2. helper is events-None-tolerant and coordinator-None-tolerant ───────


def test_helper_is_events_and_coordinator_none_tolerant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 1: ``emit_wrapped_semantic_search`` accepts ``events=None``
    (silently no-ops the emits) and ``coordinator=None`` (skips
    ``search_await`` entirely) without raising — the catalog call site's
    pre-existing degrade when ``ctx.events``/``ctx.workspace`` are unset,
    now the helper's own explicit contract (#3247 firm §6)."""
    log = EventLog(subscribers=[])
    provider = _FakeEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    items = [{"action_name": "skill__alpha", "short_description": "Alpha skill"}]
    _run(idx.build(items, op_ctx, "standard"))

    async def _scenario() -> list[dict]:
        return await emit_wrapped_semantic_search(
            events=None,
            coordinator=None,
            source_id="actions",
            index=idx,
            query="alpha",
            op_ctx=op_ctx,
            model_class="standard",
            top_k=5,
        )

    results = _run(_scenario())
    assert results, "the query itself must still serve real results"
