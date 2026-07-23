"""Tier 2: FP-0034 PR-1 universal_catalog foundation contract.

Tests for ``src/reyn/tools/universal_catalog.py`` covering:
  1. CATEGORIES taxonomy (ordered per §D18 master table).
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


def test_categories_master_table_order() -> None:
    """Tier 2: CATEGORIES order matches the §D18 master table.

    Reviewers reading the design doc and the code see the same shape.

    #3026 dropped ``memory_entry`` / ``rag_corpus`` from this table: both
    were RESOURCE categories that minted one LLM tool per stored memory /
    indexed corpus, so the enumerated payload scaled with the operator's
    data. Their capability survives as verbs in ``memory_operation`` (the
    new ``__list`` / ``__read`` pair) and ``rag_operation`` (the new
    ``__list_sources`` verb) — a fixed-count discovery action replaces the
    per-resource naming surface. See the module docstring's "four collapses"
    note.
    """
    assert CATEGORIES == (
        "multi_agent",
        # Issue #879: mcp.server / mcp.tool / mcp.operation collapsed
        # into a single ``mcp`` category whose six verb_object actions
        # cover the previous surface.
        "mcp",
        "file",
        "web",
        # #3026: ``memory_entry`` removed (was a resource category).
        "memory_operation",
        "reyn_repo",
        # #3026: ``rag_corpus`` removed (was a resource category).
        "rag_operation",
        "exec",
        # #2548 PR-C: skill management ops (install_local). NOT the ``skill__``
        # resource category; this is the management plane (mirrors ``mcp``).
        "skill_management",
        # IS-1: pipeline launch verb(s) (pipeline__run = run_pipeline).
        "pipeline",
        # pipeline management ops (install_local / install_source). NOT the
        # ``pipeline__`` resource category; management plane (mirrors
        # ``skill_management``).
        "pipeline_management",
        # proposal 0060 Phase 1 Layer A (A8): presentation management ops
        # (install). Management plane (mirrors skill_management /
        # pipeline_management); required for presentation_management__install to
        # dispatch.
        "presentation_management",
        # #3083: ADR 0064 P2 plugin management ops (install / uninstall).
        # Management plane (mirrors skill_management / pipeline_management);
        # was registered + dispatch-wired but missing from CATEGORIES, which
        # made plugin_management__install/__uninstall unreachable via every
        # enumerate-all / retrieval / codeact catalog scheme.
        "plugin_management",
    )


def test_categories_no_duplicates() -> None:
    """Tier 2: CATEGORIES taxonomy has no duplicate entries."""
    assert len(set(CATEGORIES)) == len(CATEGORIES)


def test_categories_covers_every_dispatch_wired_category() -> None:
    """Tier 2: every category with dispatch rules is enumerable via CATEGORIES.

    #3083: ``plugin_management__install`` / ``plugin_management__uninstall``
    were registered in the default ToolRegistry AND given routing rules in
    ``universal_dispatch._OPERATION_RULES``, but ``CATEGORIES`` (this
    module's closed tuple) never gained a ``"plugin_management"`` entry —
    so ``_enumerate_category`` never emitted either action into ANY
    catalog scheme's ``tools=`` payload (dogfood witness: 0/75 tools). Same
    "registered + dispatchable but catalog-invisible" class as
    #2589/#2621/#2032 (skill_management / pipeline_management /
    presentation_management all needed the identical fix previously).

    This test derives the expected category set from
    ``KNOWN_STATIC_QUALIFIED_NAMES`` — the public, registration-derived view
    of ``_OPERATION_RULES`` (the actual dispatch-routing source of truth) —
    rather than hand-listing categories a second time, so a future category
    that gains dispatch rules but not a CATEGORIES entry fails LOUD here
    instead of silently vanishing from the LLM's tool surface.
    """
    from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

    dispatch_wired_categories = {
        qualified_name.split("__", 1)[0]
        for qualified_name in KNOWN_STATIC_QUALIFIED_NAMES
    }
    missing = dispatch_wired_categories - set(CATEGORIES)
    assert not missing, (
        f"categories {sorted(missing)} have _OPERATION_RULES routing but are "
        f"missing from CATEGORIES — their actions are dispatchable via "
        f"invoke_action but will never appear in an enumerated tools= "
        f"payload (see #3083)"
    )


def test_action_categories_sp_slot_covers_every_category() -> None:
    """Tier 2: every CATEGORIES entry has an explanatory bullet in the SP.

    ``reyn.prompt.universal_slots.ACTION_CATEGORIES_LINES`` is the
    hand-maintained "## Action categories" system-prompt slot content
    (R2) — the per-category one-liner that teaches a (frequently weak)
    router model what a category is for and its qualified-name shape.
    It is a SEPARATE closed list from ``CATEGORIES`` (same #2032/#3083
    "closed enum forgot the new member" shape, just a second surface),
    so a category can be dispatch-wired and catalog-enumerable while
    still being unexplained in the prompt the LLM actually reads. #3083
    found ``plugin_management`` missing from both; this pins the SP-slot
    side so a future category addition that updates CATEGORIES but not
    this list fails here instead of leaving the LLM to guess.
    """
    from reyn.prompt.universal_slots import ACTION_CATEGORIES_LINES

    slot_text = "\n".join(ACTION_CATEGORIES_LINES)
    missing = [
        category for category in CATEGORIES
        if f"**{category}**" not in slot_text
    ]
    assert not missing, (
        f"categories {missing} have no explanatory bullet in "
        f"ACTION_CATEGORIES_LINES (the '## Action categories' SP slot)"
    )


def test_plugin_management_actions_reachable_via_catalog_entries() -> None:
    """Tier 2: plugin_management actions appear in the flat catalog payload.

    #3083 root-cause: ``catalog_entries()`` — the function every enumerate-
    all / retrieval / codeact chat scheme calls to build the LLM's ``tools=``
    — never surfaced ``plugin_management__install`` / ``__uninstall``
    because ``CATEGORIES`` omitted ``"plugin_management"``. This directly
    exercises the real production entry point (not a private accessor) with
    a minimal real ``ToolContext`` and asserts both qualified names are
    present, closing the exact reachability gap the dogfood trace witnessed
    (0/75 tools).
    """
    from reyn.tools.universal_catalog import catalog_entries

    ctx = _make_minimal_ctx()
    entries = catalog_entries(ctx)
    qualified_names = {item["name"] for item in entries}
    assert "plugin_management__install" in qualified_names
    assert "plugin_management__uninstall" in qualified_names


# ── 2. Qualified-name parse / build / validate round-trip ─────────────────


@pytest.mark.parametrize(
    "qualified, category, entry_name",
    [
        # Flat category, simple entry
        ("file__read", "file", "read"),
        ("web__search", "web", "search"),
        ("exec__run", "exec", "run"),
        # Dotted category, simple entry
        ("multi_agent__delegate", "multi_agent", "delegate"),
        # #3026: ``memory_entry__<slug>`` (a per-memory RESOURCE name) no
        # longer parses — replaced by the ``memory_operation__read`` VERB,
        # which takes ``layer`` + ``slug`` as explicit arguments instead of
        # currying the slug into the qualified name.
        ("memory_operation__read", "memory_operation", "read"),
        ("memory_operation__remember_shared", "memory_operation", "remember_shared"),
        # #3026: ``rag_corpus__<name>`` (a per-corpus RESOURCE name) no longer
        # parses — replaced by ``rag_operation__list_sources`` (discovery
        # verb, names carried in the RESULT, not the tool name) plus
        # ``rag_operation__semantic_search`` (takes ``sources`` as an arg).
        ("rag_operation__list_sources", "rag_operation", "list_sources"),
        ("rag_operation__semantic_search", "rag_operation", "semantic_search"),
        ("reyn_repo__read", "reyn_repo", "read"),
        # Issue #879 collapsed mcp surface — verb_object actions.
        ("mcp__search_registry", "mcp", "search_registry"),
        ("mcp__install_registry", "mcp", "install_registry"),
        ("mcp__list_tools", "mcp", "list_tools"),
        ("mcp__call_tool", "mcp", "call_tool"),
        ("mcp__drop_server", "mcp", "drop_server"),
        # Entry name containing further underscores (must not re-split)
        ("mcp__multi__word__name", "mcp", "multi__word__name"),
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
        # #3026: ``rag_corpus`` is gone; ``rag_operation__list_sources`` is
        # the discovery verb replacement (fixed name, no per-corpus growth).
        ("rag_operation", "list_sources", "rag_operation__list_sources"),
        # Issue #879 collapsed mcp surface.
        ("mcp", "search_registry", "mcp__search_registry"),
        ("mcp", "call_tool", "mcp__call_tool"),
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
        split_qualified_name("file__")


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
        build_qualified_name("file", "")


def test_is_valid_qualified_name_predicate() -> None:
    """Tier 2: is_valid_qualified_name returns True/False without raising.

    Mirrors the predicate convenience contract; useful in filter
    pipelines and schema validators.
    """
    assert is_valid_qualified_name("file__read") is True
    assert is_valid_qualified_name("mcp__call_tool") is True
    assert is_valid_qualified_name("missing_separator") is False
    assert is_valid_qualified_name("unknown__entry") is False
    assert is_valid_qualified_name("file__") is False


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
    """Tier 2: list_actions.category enum exposes all CATEGORIES."""
    props = LIST_ACTIONS.parameters["properties"]
    cat_items_enum = props["category"]["items"]["enum"]
    assert cat_items_enum == list(CATEGORIES)


def test_search_actions_category_enum_matches_categories() -> None:
    """Tier 2: search_actions.category enum exposes all CATEGORIES."""
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

    Per §D19, resource invoke for memory_entry / mcp__list_servers takes
    no args, so args MUST be optional. action_name is always required.
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


def test_is_search_available_membership_belt_and_suspenders() -> None:
    """Tier 2: #1454 (b) — when the known class names are supplied, a class
    NOT among them returns False (closed-world enforced at the visibility
    boundary too, not just at config-load reconciliation)."""
    classes = {"standard", "custom-alias"}
    # member → available
    assert is_search_available(
        action_retrieval_embedding_class="standard",
        embedding_class_names=classes,
    ) is True
    # non-member (dangling) → hidden, even though the string is truthy
    assert is_search_available(
        action_retrieval_embedding_class="company-proxy",
        embedding_class_names=classes,
    ) is False
    # None class stays False regardless of membership set
    assert is_search_available(
        action_retrieval_embedding_class=None,
        embedding_class_names=classes,
    ) is False


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


def test_visible_categories_includes_exec_when_sandbox_real() -> None:
    """Tier 2: visible_categories includes 'exec' when real backend present."""
    vis = visible_categories(sandbox_backend="seatbelt")
    assert "exec" in vis


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
    )


class _NullEvents:
    """Minimal events stub satisfying ToolContext.events typing."""
    subscribers: list[Any] = []

    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        pass
