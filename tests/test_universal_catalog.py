"""Tier 2: FP-0034 PR-1 universal_catalog foundation contract.

Tests for ``src/reyn/tools/universal_catalog.py`` covering:
  1. CATEGORIES taxonomy (13 entries, ordered per §D18 master table).
  2. Qualified-name parse / build / validate round-trip across
     flat (`skill`), single-dotted (`mcp.tool`), and dotted-with-
     dotted-entry (`mcp.tool__brave.search`) names.
  3. Negative cases — empty input, missing separator, unknown
     category, empty entry_name.
  4. 4 ToolDefinitions (LIST_ACTIONS / SEARCH_ACTIONS / DESCRIBE_ACTION /
     INVOKE_ACTION) shape — name, gates, render_for_router schema.
  5. D14 visibility gating predicates (is_search_available /
     is_exec_available / visible_categories) behave per §D14 /
     §D14-ext.
  6. search_actions handler degrades gracefully when no router_state
     (= real impl since Phase 2 step 1; deeper invariants in
     test_universal_handlers.py).

No mocks. No private-state assertions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates
from reyn.tools.universal_catalog import (
    CATEGORIES,
    DESCRIBE_ACTION,
    INVOKE_ACTION,
    LIST_ACTIONS,
    SEARCH_ACTIONS,
    build_qualified_name,
    is_exec_available,
    is_search_available,
    is_valid_qualified_name,
    split_qualified_name,
    visible_categories,
)

# ── 1. CATEGORIES taxonomy ────────────────────────────────────────────────


def test_categories_has_thirteen_entries() -> None:
    """Tier 2: FP-0034 §D18 master taxonomy has 13 categories."""
    assert len(CATEGORIES) == 13


def test_categories_master_table_order() -> None:
    """Tier 2: CATEGORIES order matches the §D18 master table.

    Reviewers reading the design doc and the code see the same shape.
    """
    assert CATEGORIES == (
        "skill",
        "agent.peer",
        "mcp.server",
        "mcp.tool",
        "mcp.operation",
        "file",
        "web",
        "memory.entry",
        "memory.operation",
        "reyn.source",
        "rag.corpus",
        "rag.operation",
        "exec",
    )


def test_categories_no_duplicates() -> None:
    """Tier 2: CATEGORIES taxonomy has no duplicate entries."""
    assert len(set(CATEGORIES)) == len(CATEGORIES)


# ── 2. Qualified-name parse / build / validate round-trip ─────────────────


@pytest.mark.parametrize(
    "qualified, category, entry_name",
    [
        # Flat category, simple entry
        ("skill__code_review", "skill", "code_review"),
        ("file__read", "file", "read"),
        ("web__search", "web", "search"),
        ("exec__sandboxed_exec", "exec", "sandboxed_exec"),
        # Dotted category, simple entry
        ("agent.peer__alice", "agent.peer", "alice"),
        ("mcp.server__brave", "mcp.server", "brave"),
        ("mcp.operation__drop_server", "mcp.operation", "drop_server"),
        ("memory.entry__pref_dates", "memory.entry", "pref_dates"),
        ("memory.operation__remember_shared", "memory.operation", "remember_shared"),
        ("rag.corpus__meetings", "rag.corpus", "meetings"),
        ("rag.operation__recall", "rag.operation", "recall"),
        ("reyn.source__read", "reyn.source", "read"),
        # Dotted category WITH dotted entry name (MCP tools)
        ("mcp.tool__brave.search", "mcp.tool", "brave.search"),
        ("mcp.tool__github.create_issue", "mcp.tool", "github.create_issue"),
        # Entry name containing further underscores (must not re-split)
        ("skill__multi__word__name", "skill", "multi__word__name"),
    ],
)
def test_split_qualified_name_parses_correctly(
    qualified: str, category: str, entry_name: str,
) -> None:
    """Tier 2: split_qualified_name parses §D18 examples correctly.

    Splits on FIRST ``__`` so dotted entry names and multi-underscore
    entries round-trip.
    """
    assert split_qualified_name(qualified) == (category, entry_name)


@pytest.mark.parametrize(
    "category, entry_name, expected",
    [
        ("skill", "code_review", "skill__code_review"),
        ("mcp.tool", "brave.search", "mcp.tool__brave.search"),
        ("rag.corpus", "meetings", "rag.corpus__meetings"),
        ("mcp.operation", "drop_server", "mcp.operation__drop_server"),
    ],
)
def test_build_qualified_name_round_trips(
    category: str, entry_name: str, expected: str,
) -> None:
    """Tier 2: build_qualified_name is inverse of split_qualified_name."""
    built = build_qualified_name(category, entry_name)
    assert built == expected
    # Round-trip
    assert split_qualified_name(built) == (category, entry_name)


# ── 3. Negative cases ─────────────────────────────────────────────────────


def test_split_qualified_name_missing_separator_raises() -> None:
    """Tier 2: missing __ separator raises ValueError."""
    with pytest.raises(ValueError, match=r"missing.*separator"):
        split_qualified_name("skill_code_review")


def test_split_qualified_name_unknown_category_raises() -> None:
    """Tier 2: category not in CATEGORIES raises ValueError."""
    with pytest.raises(ValueError, match="unknown category"):
        split_qualified_name("nonexistent__something")


def test_split_qualified_name_empty_entry_raises() -> None:
    """Tier 2: empty entry_name raises ValueError."""
    with pytest.raises(ValueError, match="empty entry_name"):
        split_qualified_name("skill__")


def test_split_qualified_name_non_string_raises() -> None:
    """Tier 2: non-string input raises ValueError."""
    with pytest.raises(ValueError, match="must be str"):
        split_qualified_name(42)  # type: ignore[arg-type]


def test_build_qualified_name_unknown_category_raises() -> None:
    """Tier 2: build_qualified_name rejects unknown category."""
    with pytest.raises(ValueError, match="unknown category"):
        build_qualified_name("nonexistent", "x")


def test_build_qualified_name_empty_entry_raises() -> None:
    """Tier 2: build_qualified_name rejects empty entry_name."""
    with pytest.raises(ValueError, match="non-empty"):
        build_qualified_name("skill", "")


def test_is_valid_qualified_name_predicate() -> None:
    """Tier 2: is_valid_qualified_name returns True/False without raising.

    Mirrors the predicate convenience contract; useful in filter
    pipelines and schema validators.
    """
    assert is_valid_qualified_name("skill__code_review") is True
    assert is_valid_qualified_name("mcp.tool__brave.search") is True
    assert is_valid_qualified_name("missing_separator") is False
    assert is_valid_qualified_name("unknown__entry") is False
    assert is_valid_qualified_name("skill__") is False


# ── 4. 4 ToolDefinitions shape ────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool, expected_name",
    [
        (LIST_ACTIONS, "list_actions"),
        (SEARCH_ACTIONS, "search_actions"),
        (DESCRIBE_ACTION, "describe_action"),
        (INVOKE_ACTION, "invoke_action"),
    ],
)
def test_universal_tool_names_match_fp34_spec(
    tool: ToolDefinition, expected_name: str,
) -> None:
    """Tier 2: ToolDefinition.name matches FP-0034 §Universal Catalog Wrappers."""
    assert tool.name == expected_name


@pytest.mark.parametrize(
    "tool",
    [LIST_ACTIONS, SEARCH_ACTIONS, DESCRIBE_ACTION, INVOKE_ACTION],
)
def test_universal_tools_are_router_allow_phase_deny(
    tool: ToolDefinition,
) -> None:
    """Tier 2: universal wrappers are router-only (FP-0034 §D21).

    The 4 wrappers are catalog discovery surfaces for the router; the
    phase has direct op access via Control IR allowed_ops and does not
    need the wrappers.
    """
    assert tool.gates == ToolGates(router="allow", phase="deny")


@pytest.mark.parametrize(
    "tool",
    [LIST_ACTIONS, SEARCH_ACTIONS, DESCRIBE_ACTION, INVOKE_ACTION],
)
def test_universal_tools_render_for_router_shape(
    tool: ToolDefinition,
) -> None:
    """Tier 2: render_for_router produces OpenAI tool[] shape (ADR-0026).

    Contract:
      - top-level ``type == "function"``
      - nested ``function.name``, ``function.description``, ``function.parameters``
      - ``function.parameters.type == "object"``
    """
    rendered = tool.render_for_router()
    assert rendered["type"] == "function"
    func = rendered["function"]
    assert func["name"] == tool.name
    assert isinstance(func["description"], str) and func["description"]
    params = func["parameters"]
    assert params["type"] == "object"
    assert "properties" in params


def test_list_actions_category_enum_matches_categories() -> None:
    """Tier 2: list_actions.category enum exposes all 13 CATEGORIES."""
    props = LIST_ACTIONS.parameters["properties"]
    cat_items_enum = props["category"]["items"]["enum"]
    assert cat_items_enum == list(CATEGORIES)


def test_search_actions_category_enum_matches_categories() -> None:
    """Tier 2: search_actions.category enum exposes all 13 CATEGORIES."""
    props = SEARCH_ACTIONS.parameters["properties"]
    cat_items_enum = props["category"]["items"]["enum"]
    assert cat_items_enum == list(CATEGORIES)


def test_search_actions_requires_query() -> None:
    """Tier 2: search_actions.query is required (FP-0034 §D11)."""
    assert "query" in SEARCH_ACTIONS.parameters.get("required", [])


def test_describe_action_requires_action_name() -> None:
    """Tier 2: describe_action.action_name is required (FP-0034 §D11)."""
    assert "action_name" in DESCRIBE_ACTION.parameters.get("required", [])


def test_invoke_action_requires_action_name_only() -> None:
    """Tier 2: invoke_action.action_name required; args optional (D19).

    Per §D19, resource invoke for memory.entry / mcp.server takes no
    args, so args MUST be optional. action_name is always required.
    """
    required = INVOKE_ACTION.parameters.get("required", [])
    assert "action_name" in required
    assert "args" not in required


def test_invoke_action_action_name_is_free_form_no_enum() -> None:
    """Tier 2: action_name is free-form string, no enum (FP-0034 §D12).

    §D12 documents the explicit decision to NOT constrain action_name
    via schema enum (= schema bloat avoidance, scale immunity). Runtime
    validation handles unknown names via error-with-suggestions.
    """
    name_prop = INVOKE_ACTION.parameters["properties"]["action_name"]
    assert name_prop["type"] == "string"
    assert "enum" not in name_prop


# ── 5. D14 visibility gating predicates ───────────────────────────────────


@pytest.mark.parametrize(
    "embedding_class, expected",
    [
        ("standard", True),
        ("light", True),
        ("custom_ollama", True),
        (None, False),
        ("", False),
    ],
)
def test_is_search_available_predicate(
    embedding_class: str | None, expected: bool,
) -> None:
    """Tier 2: search_actions visibility per §D14."""
    assert is_search_available(
        action_retrieval_embedding_class=embedding_class
    ) is expected


@pytest.mark.parametrize(
    "backend, expected",
    [
        ("seatbelt", True),
        ("landlock", True),
        ("noop", False),
        (None, False),
        ("", False),
    ],
)
def test_is_exec_available_predicate(
    backend: str | None, expected: bool,
) -> None:
    """Tier 2: exec category visibility per §D14-ext.

    `noop` and None/empty both mean "no real sandbox backend".
    """
    assert is_exec_available(sandbox_backend=backend) is expected


def test_visible_categories_drops_exec_when_sandbox_noop() -> None:
    """Tier 2: visible_categories excludes 'exec' when sandbox=noop."""
    vis = visible_categories(sandbox_backend="noop")
    assert "exec" not in vis
    assert len(vis) == 12  # 13 - exec


def test_visible_categories_includes_exec_when_sandbox_real() -> None:
    """Tier 2: visible_categories includes 'exec' when real backend present."""
    vis = visible_categories(sandbox_backend="seatbelt")
    assert "exec" in vis
    assert len(vis) == 13


def test_visible_categories_drops_exec_when_sandbox_none() -> None:
    """Tier 2: visible_categories excludes 'exec' when sandbox unset."""
    vis = visible_categories(sandbox_backend=None)
    assert "exec" not in vis


# ── 6. search_actions Phase 2 step 1 — handler is real, not a stub ───────


def test_search_actions_no_router_state_returns_empty() -> None:
    """Tier 2: search_actions degrades to empty when no router_state.

    Phase 2 step 1 replaced the NotImplementedError stub with a real
    handler that consults ``ctx.router_state.action_embedding_index``.
    When router_state is None (= narrow test contexts / pre-Phase-2
    invocation paths), the handler returns ``{items: [], total: 0}``
    instead of raising — gracefully degrading per §D14.

    The deeper handler invariants (query routing, ranking, category
    filter) live in tests/test_universal_handlers.py with a real
    ActionEmbeddingIndex + fake EmbeddingProvider.
    """
    ctx = _make_minimal_ctx()
    result = asyncio.run(SEARCH_ACTIONS.handler({"query": "x"}, ctx))
    assert result == {"items": [], "total": 0}


def _make_minimal_ctx() -> ToolContext:
    """Build a minimal ToolContext for stub-handler tests.

    All fields use None/empty stand-ins because the handlers raise
    NotImplementedError before consulting any context state.
    """
    return ToolContext(
        events=_NullEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
        phase_state=None,
    )


class _NullEvents:
    """Minimal events stub satisfying ToolContext.events typing."""
    subscribers: list[Any] = []

    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        pass
