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
  5. D14 visibility gating predicate (is_search_available) behaves per
     §D14; #4932 (2026-08-19, owner ruling): exec's own former gate
     (is_exec_available / D14-ext) is retired — exec is now ALWAYS
     enumerated, and is_exec_isolated (renamed, same value semantics)
     only composes an isolation-disclosure text suffix.
  6. search_actions handler degrades gracefully when no router_state
     (= real impl since Phase 2 step 1; deeper invariants in
     test_universal_handlers.py).

No mocks. No private-state assertions.
"""

from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.tools.types import RouterCallerState, ToolContext, ToolDefinition, ToolGates
from reyn.tools.universal_catalog import (
    CATEGORIES,
    DESCRIBE_ACTION,
    INVOKE_ACTION,
    LIST_ACTIONS,
    SEARCH_ACTIONS,
    _enumerate_category,
    is_exec_isolated,
    is_search_available,
    visible_categories,
)

# ── 1. CATEGORIES taxonomy ────────────────────────────────────────────────


def test_categories_master_table_order() -> None:
    """Tier 2: CATEGORIES order matches the §D18 master table.

    Reviewers reading the design doc and the code see the same shape.

    #3026 dropped ``memory_entry`` / ``rag_corpus`` from this table: both
    were RESOURCE categories that minted one LLM tool per stored memory /
    indexed corpus, so the enumerated payload scaled with the operator's
    data. ``memory_entry``'s capability survives as verbs in
    ``memory_operation`` (the new ``__list`` / ``__read`` pair) — a
    fixed-count discovery action replaces the per-resource naming surface.
    ``rag_corpus``'s #3026 operation-category replacement (``rag_operation``)
    was itself retired outright in FP-0066 P1b, along with the layer-1 agent
    tools it routed to — there is no in-core-RAG category any more. See the
    module docstring's "four collapses" note.

    #3465 appended ``embedding`` (the ``embed`` primitive) and ``hooks``
    (``emit_hook_event`` / ``hooks_add``) — both were registered
    router=allow + dispatch-wired but missing a CATEGORIES entry, the same
    #3083-class gap the ``plugin_management`` comment below documents.
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
        # FP-0066 P1b: ``rag_operation`` retired outright (was #3026's
        # replacement for the removed ``rag_corpus`` resource category).
        "exec",
        # #2548 PR-C: skill management ops (install_local). NOT the ``skill__``
        # resource category; this is the management plane (mirrors ``mcp``).
        "skill_management",
        # IS-1: pipeline launch verb(s) (run_pipeline = run_pipeline).
        "pipeline",
        # pipeline management ops (install_local / install_source). NOT the
        # ``pipeline__`` resource category; management plane (mirrors
        # ``skill_management``).
        "pipeline_management",
        # proposal 0067 P4 (#3978): describe_task / list_tasks / cancel_task —
        # read/act against a currently-running async task's settle-path handle.
        "task",
        # proposal 0060 Phase 1 Layer A (A8): presentation management ops
        # (install). Management plane (mirrors skill_management /
        # pipeline_management); required for presentation_install_local to
        # dispatch.
        "presentation_management",
        # #3083: ADR 0064 P2 plugin management ops (install / uninstall).
        # Management plane (mirrors skill_management / pipeline_management);
        # was registered + dispatch-wired but missing from CATEGORIES, which
        # made install_plugin/__uninstall unreachable via every
        # enumerate-all / retrieval / codeact catalog scheme.
        "plugin_management",
        # FP-0066 P3c (#3247 firm §3): the ``knowledge`` category —
        # search_knowledge / search_knowledge, semantic search over the
        # operator's own skill/memory/repo knowledge. Single-entry, like
        # ``exec`` — runtime-gated on ``embedding.enabled`` rather than a
        # sandbox backend.
        "knowledge",
        # #3465: ``embed`` — the raw, USER-FACING batch text->vector
        # primitive. Registered + dispatch-wired but missing from
        # CATEGORIES until now.
        "embedding",
        # #3465: ``emit_hook_event`` / ``hooks_add`` — both already declared
        # ``ToolDefinition(category="hooks")``; registered + dispatch-wired
        # but missing from CATEGORIES until now.
        "hooks",
    )


def test_categories_no_duplicates() -> None:
    """Tier 2: CATEGORIES taxonomy has no duplicate entries."""
    assert len(set(CATEGORIES)) == len(CATEGORIES)


def test_categories_covers_every_dispatch_wired_category() -> None:
    """Tier 2: every category with dispatch rules is enumerable via CATEGORIES.

    #3083: ``install_plugin`` / ``uninstall_plugin``
    were registered in the default ToolRegistry AND listed as catalog
    actions, but ``CATEGORIES`` (this
    module's closed tuple) never gained a ``"plugin_management"`` entry —
    so ``_enumerate_category`` never emitted either action into ANY
    catalog scheme's ``tools=`` payload (dogfood witness: 0/75 tools). Same
    "registered + dispatchable but catalog-invisible" class as
    #2589/#2621/#2032 (skill_management / pipeline_management /
    presentation_management all needed the identical fix previously).

    This test derives the expected category set from the membership table's
    own ``category_of`` view — the actual dispatch source of truth — rather
    than hand-listing categories a second time, so a future category that
    gains actions but not a CATEGORIES entry fails LOUD here instead of
    silently vanishing from the LLM's tool surface. (#3429: the derivation
    used to split the category out of each qualified NAME; the category is now
    a table lookup, so the same fact is read rather than parsed.)
    """
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES_SORTED, category_of

    dispatch_wired_categories = {
        category_of(name) for name in KNOWN_ACTION_NAMES_SORTED
    }
    missing = dispatch_wired_categories - set(CATEGORIES)
    assert not missing, (
        f"categories {sorted(missing)} have catalog actions but are "
        f"missing from CATEGORIES — their actions are dispatchable via "
        f"invoke_action but will never appear in an enumerated tools= "
        f"payload (see #3083)"
    )


def test_every_dispatch_wired_category_actually_enumerates() -> None:
    """Tier 2: every dispatch-wired category ENUMERATES its verbs — not just
    that its name is listed in CATEGORIES.

    #4154: ``task`` passed ``test_categories_covers_every_dispatch_wired_category``
    above (it WAS in ``CATEGORIES``) while remaining permanently invisible to
    every catalog scheme — ``_enumerate_category("task", ctx)`` had no
    matching branch and fell through to an unconditional ``return []``. The
    #3083 test's own derivation (membership in the tuple) cannot catch this
    class of gap; it needs the CALL made, not the name looked up. This test
    calls ``_enumerate_category`` for every dispatch-wired category, under
    conditions favorable to every runtime gate (a real sandbox backend +
    embedding configured), and asserts each comes back non-empty.

    Real ``ToolContext`` + real ``RouterCallerState`` (populated, not a hand
    stub) — no mocks.
    """
    from reyn.tools.types import RouterCallerState
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES_SORTED, category_of

    dispatch_wired_categories = {
        category_of(name) for name in KNOWN_ACTION_NAMES_SORTED
    }
    # Guard the derivation itself (lead-coder review, mirroring #4153's
    # vacuity guard): if this set is empty, every category below would
    # trivially pass with nothing checked.
    assert dispatch_wired_categories, (
        "dispatch-wired category derivation returned nothing — the gate "
        "would pass vacuously"
    )
    favorable_state = RouterCallerState(
        excluded_categories=frozenset(),
        sandbox_backend="seatbelt",
        embedding_provider=object(),
        embedding_model_class="some-model",
    )
    ctx = ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=favorable_state,
    )
    empty = [
        cat for cat in sorted(dispatch_wired_categories)
        if _enumerate_category(cat, ctx) == []
    ]
    assert empty == [], (
        f"categories {empty} are dispatch-wired and listed in CATEGORIES "
        f"but _enumerate_category returns [] even under favorable "
        f"conditions — dispatchable via invoke_action, undiscoverable via "
        f"list_actions/tools= (see #4154)"
    )


def test_action_categories_sp_slot_covers_every_category() -> None:
    """Tier 2: every CATEGORIES entry has an explanatory bullet in the SP.

    ``reyn.prompt.universal_slots.ACTION_CATEGORIES_LINES`` is the
    hand-maintained "## Action categories" system-prompt slot content
    (R2) — the per-category one-liner that teaches a (frequently weak)
    router model what a category is for.
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
    — never surfaced ``install_plugin`` / ``__uninstall``
    because ``CATEGORIES`` omitted ``"plugin_management"``. This directly
    exercises the real production entry point (not a private accessor) with
    a minimal real ``ToolContext`` and asserts both action names are
    present, closing the exact reachability gap the dogfood trace witnessed
    (0/75 tools).
    """
    from reyn.tools.universal_catalog import catalog_entries

    ctx = _make_minimal_ctx()
    entries = catalog_entries(ctx)
    action_names = {item["name"] for item in entries}
    assert "install_plugin" in action_names
    assert "uninstall_plugin" in action_names


def test_embed_and_hooks_actions_reachable_via_catalog_entries() -> None:
    """Tier 2: ``embed`` / ``emit_hook_event`` / ``hooks_add`` appear in the
    flat catalog payload (#3465).

    Same #3083-class gap ``test_plugin_management_actions_reachable_via_catalog_entries``
    closes: all 3 were registered router=allow with a module docstring
    already asserting LLM-facing intent, but ``_CATEGORY_ACTIONS`` /
    ``CATEGORIES`` never gained an entry for them, so #3464's
    reachability gate flagged them ``DEFERRED_WIRING_BUG``. This exercises
    the real production entry point (not a private accessor) and asserts
    all 3 names are present in the enumerated ``tools=`` payload — the
    advertised-surface half of the fix (route (b) membership alone would
    make them dispatchable but not discoverable).
    """
    from reyn.tools.universal_catalog import catalog_entries

    ctx = _make_minimal_ctx()
    entries = catalog_entries(ctx)
    action_names = {item["name"] for item in entries}
    assert "embed" in action_names
    assert "emit_hook_event" in action_names
    assert "hooks_add" in action_names


# ── 2. Action names carry no catalog separator (#3429) ───────────────────
#
# Sections 2 and 3 used to pin the ``<category>__<entry>`` parser
# (``split_qualified_name`` / ``build_qualified_name`` /
# ``is_valid_qualified_name``): round-trips, unknown-category rejection, the
# "an entry name may itself contain ``__``" rule, and so on. The parser is
# deleted with the spelling it parsed, so those tests are deleted rather than
# adapted — there is no second name to parse OUT of.
#
# What replaces them is the property the parser's existence made impossible to
# state: no name the catalog offers contains the separator at all.


def test_no_category_name_contains_the_separator() -> None:
    """Tier 2: #3429 — a category name is a browsing label, never a prefix.

    ``list_actions(category=[…])`` is where a category reaches the model. A
    ``__`` in one would be the qualified spelling growing back from the other
    end (``file__ops__read``), so it fails here."""
    offenders = [c for c in CATEGORIES if "__" in c]
    assert offenders == [], f"category name(s) contain the separator: {offenders}"


def test_every_catalog_action_is_directly_dispatchable() -> None:
    """Tier 2: #3429 — every catalog action is in ``REGISTRY_DISPATCH_TOOLS``.

    ``_invoke_router_tool`` used to have a second arm: a name containing ``__``
    was an action's QUALIFIED spelling, which the dispatch set did not carry, so
    it was re-routed through ``invoke_action``. 23 of the 46 actions depended on
    that arm entirely — they were advertised by the flat catalog under their
    qualified name and dispatched by the wrapper hop, so their ToolDefinitions
    never set ``router_dispatched``.

    Deleting the arm turned that into a live break: an advertised action without
    the flag reaches the "unhandled tool" safety return. It was caught by a
    Tier-3 replay fixture (``test_fp0063_arc_witness``) rather than by a unit
    check, which is exactly the #2120/#2122 "wired at one seam, not the others"
    class ``router_dispatched`` was introduced to kill — so the invariant is
    stated here, over the whole action set, rather than left to a fixture."""
    from reyn.runtime.router_loop import RouterLoop
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

    assert KNOWN_ACTION_NAMES, "no actions — this check would be vacuous"
    missing = sorted(KNOWN_ACTION_NAMES - RouterLoop.REGISTRY_DISPATCH_TOOLS)
    assert missing == [], (
        f"catalog action(s) not in REGISTRY_DISPATCH_TOOLS: {missing}. A direct "
        f"call to one of these lands on 'unhandled tool'. Set "
        f"router_dispatched=True on the ToolDefinition."
    )


def test_no_enumerated_action_name_contains_the_separator() -> None:
    """Tier 2: #3429 — nothing the catalog ENUMERATES is qualified.

    Read off the live ``catalog_entries`` payload (the flat action list every
    scheme sends as ``tools=``) rather than off the membership table, so this
    fails on a name minted anywhere between the table and the wire."""
    from reyn.tools.universal_catalog import catalog_entries

    entries = catalog_entries(_make_minimal_ctx())
    assert entries, "catalog_entries produced nothing — the check would be vacuous"
    offenders = sorted(e["name"] for e in entries if "__" in e["name"])
    assert offenders == [], f"qualified name(s) in the enumerated catalog: {offenders}"



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
    assert tool.gates == ToolGates(router="allow")


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

    Per §D19, resource invoke for memory_entry / list_mcp_servers takes
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
    "embedding_enabled, expected",
    [
        (True, True),
        (False, False),
    ],
)
def test_is_search_available_predicate(
    embedding_enabled: bool, expected: bool,
) -> None:
    """Tier 2: search_actions visibility per §D14 / FP-0066 §7.

    Clean-break replacement for the retired ``action_retrieval.embedding_class``
    truthy + closed-world-membership predicate: the on/off decision is now a
    single bool (``embedding.enabled``) — the embedding CLASS itself is
    validated eagerly at config-load time (``_build_embedding_config``), so
    this predicate no longer needs a membership check.
    """
    assert is_search_available(embedding_enabled=embedding_enabled) is expected


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
def test_is_exec_isolated_predicate(
    backend: str | None, expected: bool,
) -> None:
    """Tier 2: #4932 — is_exec_isolated (renamed from is_exec_available,
    same value semantics) reports whether real isolation applies.

    `noop` and None/empty both mean "no real sandbox backend" (= not
    isolated). Since #4932 this is DISCLOSURE-only — it no longer gates
    exec's visibility (see the visible_categories/enumerate tests below).
    """
    assert is_exec_isolated(sandbox_backend=backend) is expected


def test_visible_categories_always_includes_exec() -> None:
    """Tier 2: #4932 (owner ruling, 2026-08-19) — visible_categories no
    longer drops 'exec' for any sandbox_backend value; category
    visibility follows the same permission axis every other category
    uses (gates.router + exec: allow), never sandbox posture. Strip-
    falsify note: this test would have failed under the pre-#4932 gate
    for every value below except a real backend."""
    for backend in ("seatbelt", "landlock", "noop", None, ""):
        assert "exec" in visible_categories(), (
            f"exec must be visible regardless of sandbox_backend "
            f"(checked implicitly for {backend!r} — visible_categories "
            f"no longer takes a sandbox_backend argument at all)"
        )


def test_enumerate_exec_always_returns_the_action_regardless_of_backend() -> None:
    """Tier 2: #4932 — _enumerate_category('exec', ...) returns the exec
    action for every sandbox_backend value, including noop/None (the
    exact cases D14-ext used to hide it for)."""
    for backend in ("seatbelt", "noop", None):
        ctx = ToolContext(
            events=None,
            permission_resolver=None,
            workspace=None,
            caller_kind="router",
            router_state=RouterCallerState(sandbox_backend=backend),
        )
        actions = _enumerate_category("exec", ctx)
        assert [a["action_name"] for a in actions] == ["exec"], (
            f"exec must always enumerate, got {actions!r} for backend={backend!r}"
        )


def test_enumerate_exec_discloses_no_isolation_when_noop_or_none() -> None:
    """Tier 2: #4932 — when isolation does not apply, the short_description
    the LLM sees names it explicitly (never silence — the replacement for
    hiding the category)."""
    for backend in ("noop", None):
        ctx = ToolContext(
            events=None,
            permission_resolver=None,
            workspace=None,
            caller_kind="router",
            router_state=RouterCallerState(sandbox_backend=backend),
        )
        actions = _enumerate_category("exec", ctx)
        assert "no sandbox isolation is applied" in actions[0]["short_description"], (
            f"expected an explicit isolation-disclosure notice for "
            f"backend={backend!r}, got {actions[0]['short_description']!r}"
        )


def test_enumerate_exec_no_disclosure_when_real_backend() -> None:
    """Tier 2: #4932 — with a real sandbox backend, no isolation-disclosure
    suffix is appended (nothing to disclose)."""
    ctx = ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(sandbox_backend="seatbelt"),
    )
    actions = _enumerate_category("exec", ctx)
    assert "no sandbox isolation" not in actions[0]["short_description"]


# ── 6. search_actions Phase 2 step 1 — handler is real, not a stub ───────


def test_search_actions_no_router_state_returns_empty() -> None:
    """Tier 2: search_actions degrades to empty when no router_state.

    Phase 2 step 1 replaced the NotImplementedError stub with a real
    handler that consults ``ctx.router_state.action_embedding_index``.
    When router_state is None (= narrow test contexts / pre-Phase-2
    invocation paths), the handler returns ``{items: [], total: 0}``
    instead of raising — gracefully degrading per §D14.

    The deeper handler invariants (query routing, ranking, category
    filter) live in tests/tools/test_universal_handlers.py with a real
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
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
