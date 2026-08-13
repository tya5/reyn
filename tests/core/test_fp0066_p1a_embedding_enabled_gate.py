"""Tier 2: FP-0066 P1a — `embedding.enabled` config clean-break (#3218 umbrella part).

Covers the parts of the ratified retrieval redesign
(`docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md` §7) not
already exercised by ``tests/config/test_embedding_config.py`` (the field/parser
itself) or the migrated ``tests/tools/test_universal_catalog.py`` /
``tests/tools/test_2895_retrieval_scheme_requires_embedding.py`` (the
``is_search_available`` predicate + the retrieval-scheme config-load gate):

  1. Symmetric model (§7 table): ``embedding.enabled: false`` hides only the
     semantic-discovery layer (``search_actions``) — non-semantic discovery
     (``list_actions``) and load/invoke verbs (``invoke_action``) still work.
  2. §G9: the OS-internal `embed` op pre-flights ``embedding.enabled`` and
     returns a decision-enabling ``status="blocked"`` block when it is off,
     rather than a silent no-op or an opaque provider error — the single
     contact point that gives the FP-0063 plugin's `rag_ingest` (which calls
     the `embed` tool) a clear block with no plugin-side edit.
  3. #3218: ``ReynConfig.mcp_search_threshold`` + its parser are gone
     (fold-removed, confirmed no-op).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import EmbedIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    DESCRIBE_ACTION,
    INVOKE_ACTION,
    LIST_ACTIONS,
    SEARCH_ACTIONS,
    is_search_available,
)
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _run(coro):
    return asyncio.run(coro)


def _ctx(rs: RouterCallerState | None) -> ToolContext:
    return ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


# ---------------------------------------------------------------------------
# 1. Symmetric model — embedding.enabled gates ONLY semantic discovery
# ---------------------------------------------------------------------------


def test_embedding_disabled_hides_search_actions_only() -> None:
    """Tier 2: §7 symmetric model — `embedding.enabled=False` predicate hides
    search_actions (the semantic-discovery layer), full stop."""
    assert is_search_available(embedding_enabled=False) is False


def test_embedding_enabled_exposes_search_actions() -> None:
    """Tier 2: §7 symmetric model — `embedding.enabled=True` predicate
    exposes search_actions."""
    assert is_search_available(embedding_enabled=True) is True


def test_list_actions_works_regardless_of_embedding_enabled() -> None:
    """Tier 2: §7 symmetric model — non-semantic discovery (`list_actions`)
    is unaffected by `embedding.enabled` — a router_state with no embedding
    wired (= embedding.enabled: false in production) still returns a full
    catalog page, not an error/empty result."""
    rs = RouterCallerState(
        action_embedding_index=None,
        embedding_provider=None,
        embedding_model_class=None,
    )
    result = _run(LIST_ACTIONS.handler({"category": ["file"]}, _ctx(rs)))
    assert "items" in result
    assert result["total"] > 0


def test_invoke_action_reachable_regardless_of_embedding_enabled() -> None:
    """Tier 2: §7 symmetric model — invoke_action (the `action` group's
    activation verb) does not require embedding at all; its schema/gate
    carries no embedding dependency."""
    # invoke_action's ToolDefinition itself declares no embedding
    # precondition — the ONLY embedding-conditioned wrapper is search_actions
    # (per build_tools' §D14 gating in router_tools.py). Asserting on the
    # descriptor (rather than driving a full dispatch, which needs a real
    # registry entry to invoke) keeps this Tier 2 / dependency-free.
    assert INVOKE_ACTION.name == "invoke_action"
    assert "embedding" not in INVOKE_ACTION.description.lower()
    assert "embedding" not in DESCRIBE_ACTION.description.lower()


def test_search_actions_description_names_new_config_key() -> None:
    """Tier 2: search_actions' own description (the LLM-facing surface
    gating information) names `embedding.enabled`, not the retired
    `action_retrieval.embedding_class` — regression guard for the
    tool-description migration (tests/tools/test_tool_description_relocation.py
    pins the byte-identical baseline; this pins the SEMANTIC content)."""
    assert "embedding.enabled" in SEARCH_ACTIONS.description
    assert "action_retrieval.embedding_class" not in SEARCH_ACTIONS.description


# ---------------------------------------------------------------------------
# 2. §G9 — the `embed` op pre-flight block when embedding.enabled is False
# ---------------------------------------------------------------------------


def _embed_ctx() -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


def test_embed_op_fails_closed_on_unloadable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: architect co-vet ruling (#3256) — an unloadable config makes
    the `embed` op fail **CLOSED** (the §G9 block), not open.

    A config-load failure must not silently enable a cost-bearing, opt-in
    capability the operator never configured (cost-safety); "cannot confirm
    enabled" must resolve the same way "confirmed disabled" does (opt-in
    symmetry with the field-absent default) — the same "unreadable → deny"
    shape as #3201 (identity unreadable → floor) and #3227 (can't confirm →
    deny). Driven through the PUBLIC `handle()` entry point (not the
    private `_is_embedding_enabled` helper) so the assertion is on real
    op-level behavior, not private state. Narrow monkeypatch of
    ``load_config`` itself (raises) — NOT a faked config object — is the
    seam under test.

    ``monkeypatch.undo()`` first reverts the suite-wide autouse
    ``_embedding_enabled_by_default`` fixture (conftest.py), which patches
    the private ``_is_embedding_enabled`` helper directly to keep the rest
    of the suite green — this test needs the REAL implementation (which
    calls ``load_config``) under test, not that stand-in.
    """
    import reyn.config as config_mod
    import reyn.core.op_runtime.embed as embed_mod

    monkeypatch.undo()  # restore the real _is_embedding_enabled (see docstring)

    def _raise() -> None:
        raise RuntimeError("simulated unreadable reyn.yaml")

    monkeypatch.setattr(config_mod, "load_config", _raise)

    result = asyncio.run(
        embed_mod.handle(
            EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard"),
            _embed_ctx(),
        )
    )
    assert result["status"] == "blocked"
    assert "embedding.enabled: true" in result["error"]


def test_embed_op_blocks_with_decision_enabling_message_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: §G9 — with `embedding.enabled` False, the `embed` op returns a
    decision-enabling `status="blocked"` block (not a silent empty result,
    not an opaque exception) naming the fix (`embedding.enabled: true`).

    This is the ONLY contact point the FP-0063 `rag_ingest` pipeline (which
    calls the `embed` tool directly) needs for a clear block — no plugin-side
    edit required, since every embedding egress in the OS funnels through
    this one op (per `embed.py`'s own module docstring).
    """
    import reyn.core.op_runtime.embed as embed_mod

    monkeypatch.setattr(embed_mod, "_is_embedding_enabled", lambda: False)

    result = asyncio.run(
        embed_mod.handle(
            EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard"),
            _embed_ctx(),
        )
    )
    assert result["status"] == "blocked"
    assert result["kind"] == "embed"
    assert "embedding.enabled: true" in result["error"]


def test_embed_op_proceeds_normally_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 1: §G9 contrast — `embedding.enabled` True does not block; the op
    reaches the provider call (proven by a real, deterministic fake provider
    returning a real vector, not a mocked assertion)."""
    import reyn.core.op_runtime.embed as embed_mod
    from reyn.data.embedding.provider import EmbedBatchResult

    class _FakeProvider:
        async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
            return EmbedBatchResult(vectors=[[1.0, 2.0]], model=model, total_tokens=1)

        def estimate_tokens(self, texts: list[str]) -> int:
            return len(texts)

        def get_dimension(self, model: str) -> int:
            return 2

    monkeypatch.setattr(embed_mod, "_is_embedding_enabled", lambda: True)
    monkeypatch.setattr(embed_mod, "get_provider", lambda *a, **kw: _FakeProvider())

    result = asyncio.run(
        embed_mod.handle(
            EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard"),
            _embed_ctx(),
        )
    )
    assert result.get("status") != "blocked"
    assert result["vectors"] == [[1.0, 2.0]]


# ---------------------------------------------------------------------------
# 3. #3218 — mcp_search_threshold fold-removal
# ---------------------------------------------------------------------------


def test_mcp_search_threshold_field_is_gone() -> None:
    """Tier 1: #3218 — `ReynConfig.mcp_search_threshold` no longer exists on
    the dataclass (clean-break removal of the confirmed no-op field)."""
    import dataclasses

    from reyn.config import ReynConfig

    field_names = {f.name for f in dataclasses.fields(ReynConfig)}
    assert "mcp_search_threshold" not in field_names


def test_parse_mcp_search_threshold_helper_is_gone() -> None:
    """Tier 1: #3218 — the loader's `_parse_mcp_search_threshold` helper is
    removed alongside the field it fed."""
    from reyn.config import loader as loader_mod

    assert not hasattr(loader_mod, "_parse_mcp_search_threshold")


def test_bare_mcp_search_threshold_key_is_ignored_not_erroring(tmp_path) -> None:
    """Tier 2: a bare `mcp.search_threshold:` in reyn.yaml is now purely inert
    (an unread free-form sub-key of the raw `mcp:` dict) rather than erroring
    — matching the existing unknown-sub-key policy every other undeclared
    `mcp:` key already gets (the section stays a raw, unvalidated dict)."""
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML + "mcp:\n  search_threshold: 5\n", encoding="utf-8",
    )
    cfg = load_config(cwd=tmp_path)
    # No derived field reads it anymore; it just survives in the raw dict.
    assert cfg.mcp.get("search_threshold") == 5
    assert not hasattr(cfg, "mcp_search_threshold")


# ---------------------------------------------------------------------------
# Sanity: `embedding_class` never resurfaces as a live field anywhere.
# ---------------------------------------------------------------------------
# (#4552 PR-3+4: the ``ActionRetrievalConfig`` this sanity check used to
# target — the clean-break split's OTHER half, `embedding.enabled` +
# `embedding.default_class` — is deleted entirely, its own 4 fields
# fully migrated/retired across the #4552 arc. Nothing left to pin here;
# `test_search_actions_description_names_new_config_key` above already
# guards the retired ``action_retrieval.embedding_class`` key from
# resurfacing in the LLM-facing description.)
