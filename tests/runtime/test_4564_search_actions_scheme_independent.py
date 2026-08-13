"""Tier 2: #4564 strip-falsify witness — ``universal_wrappers_enabled``'s
reach is CONTAINED to the ``universal-category`` scheme's own 3 non-search
wrapper functions; ``search_actions`` visibility is governed by
``embedding.enabled`` alone, regardless of scheme.

Before #4564, ``RouterLoop.run()`` computed ``_search_visible`` (the D14
gate every scheme's ``present()``/``build_presentation`` call reads via
``layer_ctx["search_visible"]``) ONLY inside an ``if _univ_enabled:``
block — an undeclared second gate on top of ``embedding.enabled``
(``embedding.py``'s own docstring: "search_actions is gated separately
via ``embedding.enabled``"). Setting ``universal_wrappers_enabled: false``
while using the ``retrieval`` scheme silently hid ``search_actions`` too,
even though the flag's name reads as scoped to ``universal-category``, a
scheme the operator may not even be using.

Scheme choice: NOT ``enumerate-all`` — that scheme's own
``base_tools()`` (``router_loop.py``'s ``SchemeOps`` adapter) hardcodes
``search_actions_visible=False`` unconditionally by design (enumerate-all
exposes the flat catalog directly; a search-indirection tool would be
redundant there), so it can never witness this claim either way.
``retrieval`` is the scheme whose own ``build_presentation`` (line ~129)
genuinely branches on ``layer_ctx["search_visible"]`` to add/omit a real
``search_actions`` tool — read directly before choosing it, not assumed.

This test drives a REAL ``RouterLoop.run()`` (not a scheme-file unit test)
with ``universal_wrappers_enabled=False`` + ``scheme_name="retrieval"`` +
a real embedding build (real ``ActionEmbeddingIndex`` + ``IndexCoordinator``
+ a real, non-mock, succeeding embedding provider, same convention as
``tests/core/test_index_coordinator_3247_p2b.py``) and asserts the
CONTAINMENT claim directly on ``tools=``: ``search_actions`` IS present;
the 3 universal-category wrapper functions are NOT (retrieval's own
``base_tools()`` never carries them either — the containment claim holds
either way, but asserting their absence keeps the claim explicit). The
build is driven through the real eager-build path
(``get_eager_embedding_build`` True) rather than a hand-set
``is_ready()`` — per lead-coder's review, a hand-constructed "ready" index
would be a configuration only the test itself builds, not the actual
production build path a real turn takes.

Strip-falsify: reverting #4564's fix (re-wrapping the action-index
build+readiness block in ``if _univ_enabled:``) turns this test RED —
``search_actions`` would drop out of ``tools=`` (retrieval degrades to
the full flat catalog instead, per its own #2895 fallback) even though
``embedding.enabled`` (mirrored here by the getters returning real,
non-None values) never changed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.coordinator import IndexCoordinator
from reyn.data.workspace.workspace import Workspace
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.action_index import ActionEmbeddingIndex

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=2)


class _SucceedingEmbeddingProvider:
    """Real fake provider that returns deterministic canned vectors — same
    convention as ``tests/core/test_index_coordinator_3247_p2b.py``'s
    ``_FakeEmbeddingProvider``. No Mock/AsyncMock per policy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append(tuple(texts))
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def _op_ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch, events: EventLog) -> OpContext:
    """Real OpContext whose `embed` op resolves to ``provider`` (mirrors
    ``tests/core/test_action_embedding_build_failure_1458.py``'s
    ``_op_ctx_for``)."""
    import reyn.core.op_runtime.embed as _embed_mod

    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


class _WrappersOffSearchOnHost:
    """A real (non-mock) ``RouterLoopHost``: universal wrappers OFF, but a
    real, ready-to-build embedding index configured — the exact #4564
    combination (wrappers off, embedding on, non-universal-category
    scheme)."""

    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(
        self, *, op_ctx: OpContext, coordinator: IndexCoordinator,
        idx: ActionEmbeddingIndex, provider: Any, events: EventLog,
    ) -> None:
        self._op_ctx = op_ctx
        self._coordinator = coordinator
        self._idx = idx
        self._provider = provider
        self._events = events
        self.outbox: list[dict] = []

    @property
    def events(self) -> EventLog:
        return self._events

    def make_router_op_context(self) -> OpContext:
        return self._op_ctx

    def get_index_coordinator(self) -> IndexCoordinator:
        return self._coordinator

    # ── the #4564 combination ──────────────────────────────────────────
    def get_universal_wrappers_enabled(self) -> bool:
        return False  # wrappers OFF

    def get_action_embedding_index(self) -> ActionEmbeddingIndex:
        return self._idx  # embedding IS configured

    def get_embedding_provider(self) -> Any:
        return self._provider

    def get_embedding_model_class(self) -> str:
        return "standard"

    def get_eager_embedding_build(self) -> bool:
        # Drive the REAL synchronous build path so the index is genuinely
        # ready by the time tools= is computed within this same turn —
        # not a hand-set is_ready() (lead-coder review).
        return True

    # ── everything else RouterLoop.run() touches ───────────────────────
    def list_available_skills(self) -> list[dict]:
        return []

    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> dict | None:
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    def resolve_model(self, name: str) -> str:
        return "fake-model"

    async def put_outbox(
        self, *, kind: str, text: str, meta: dict, persist: bool = True,
    ) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def reyn_repo_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_repo_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    async def run_skill_awaitable(
        self, *, skill: str, input: dict, chain_id: str
    ) -> dict:
        return {"status": "finished", "data": {"result": f"{skill} ran"}}


def test_wrappers_off_retrieval_scheme_still_exposes_search_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #4564 strip-falsify witness (containment claim). With
    universal_wrappers_enabled=False and scheme_name="retrieval", a real
    embedding build makes search_actions appear in tools= — the 3
    universal-category wrapper functions do NOT, because that scheme never
    reads the flag for its own presentation at all."""
    events = EventLog()
    provider = _SucceedingEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, events)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    coordinator = IndexCoordinator(tmp_path)
    host = _WrappersOffSearchOnHost(
        op_ctx=op_ctx, coordinator=coordinator, idx=idx, provider=provider,
        events=events,
    )

    captured: dict = {}

    async def _capture_tools(**kwargs: object) -> LLMToolCallResult:
        captured["tools"] = kwargs.get("tools")
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
        )

    loop = RouterLoop(
        host=host, chain_id="chain-test-4564", max_iterations=5,
        scheme_name="retrieval",
    )
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _capture_tools)
    asyncio.run(loop.run("hello", []))

    assert provider.calls, (
        "the real embedding build never ran — this test would be vacuous "
        "(asserting on an index that was never actually built)"
    )
    assert idx.is_ready(), "the real build did not complete — is_ready() is False"

    tools = captured["tools"]
    assert tools is not None, "call_llm_tools was never invoked"
    names = {t["function"]["name"] for t in tools}

    assert "search_actions" in names, (
        f"search_actions must appear in tools= once embedding.enabled builds "
        f"a ready index, regardless of universal_wrappers_enabled; got {sorted(names)}"
    )
    for wrapper in ("list_actions", "describe_action", "invoke_action"):
        assert wrapper not in names, (
            f"{wrapper!r} is a universal-category-scoped wrapper — it must "
            f"NOT appear under the retrieval scheme even with "
            f"universal_wrappers_enabled=False on this host; got {sorted(names)}"
        )
