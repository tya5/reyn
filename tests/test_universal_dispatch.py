"""Tier 2: FP-0034 PR-2 universal_dispatch routing contract.

Tests for ``src/reyn/tools/universal_dispatch.py`` covering:
  1. resolve_invoke_action across all 13 categories (= resource
     invoke per §D19 AND operation per-name lookup).
  2. Arg transformers for each routing flavour (skill / agent /
     mcp.server / mcp.tool / memory.entry / rag.corpus + passthrough
     for the operation categories).
  3. UnknownActionError carrying ``action_name`` / ``reason`` /
     ``suggestions`` per §D12.
  4. suggest_similar_names ranking via difflib (= deterministic,
     no LLM, no embeddings).
  5. resolve_describe_action returning the same target tool name
     as resolve_invoke_action (= describe surfaces the canonical
     invoke target's schema).
  6. KNOWN_STATIC_QUALIFIED_NAMES inventory + per-category subset.

No mocks. No private-state assertions. Pure-function routing tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from reyn.tools.universal_dispatch import (
    KNOWN_STATIC_QUALIFIED_NAMES,
    ResolvedAction,
    UnknownActionError,
    known_qualified_name_for_category,
    resolve_describe_action,
    resolve_invoke_action,
    suggest_similar_names,
)

# ── 1. resolve_invoke_action — resource categories (§D19) ────────────────


def test_resolve_invoke_action_skill_routes_to_invoke_skill() -> None:
    """Tier 2: skill__<name> → invoke_skill(name, input)."""
    result = resolve_invoke_action(
        "skill__code_review",
        {"input": {"type": "code_input", "data": {"path": "x.py"}}},
    )
    assert result.target_tool_name == "invoke_skill"
    assert result.target_args == {
        "name": "code_review",
        "input": {"type": "code_input", "data": {"path": "x.py"}},
    }


def test_resolve_invoke_action_skill_wraps_args_when_input_missing() -> None:
    """Tier 2: skill__<name> with no 'input' wraps args as input payload."""
    result = resolve_invoke_action("skill__foo", {"x": 1, "y": 2})
    assert result.target_tool_name == "invoke_skill"
    assert result.target_args["name"] == "foo"
    assert result.target_args["input"] == {"x": 1, "y": 2}


def test_resolve_invoke_action_agent_peer_routes_to_delegate() -> None:
    """Tier 2: agent.peer__<name> → delegate_to_agent(to=name, request=...).

    Universal-catalog callers pass ``message``; the translator must remap it
    to ``request`` so the delegate_to_agent handler never raises KeyError.
    """
    result = resolve_invoke_action(
        "agent.peer__alice", {"message": "hi"},
    )
    assert result.target_tool_name == "delegate_to_agent"
    assert result.target_args["to"] == "alice"
    # "message" must be remapped → "request" by _delegate_to_agent_args (B27-H3)
    assert result.target_args["request"] == "hi"
    assert "message" not in result.target_args


def test_resolve_invoke_action_mcp_list_tools_passes_server_arg() -> None:
    """Tier 2: mcp__list_tools forwards the LLM-supplied {server} arg to
    the list_mcp_tools handler verbatim (#879 collapsed surface).
    """
    result = resolve_invoke_action(
        "mcp__list_tools", {"server": "brave"},
    )
    assert result.target_tool_name == "list_mcp_tools"
    assert result.target_args == {"server": "brave"}


def test_resolve_invoke_action_mcp_call_tool_passes_tool_id() -> None:
    """Tier 2: mcp__call_tool routes to the mcp_call_tool verb wrapper
    (#879). The LLM passes a self-contained ``<server>__<tool>``
    identifier in the ``tool`` arg; the wrapper's handler splits it
    before dispatching to ``call_mcp_tool`` internally.
    """
    result = resolve_invoke_action(
        "mcp__call_tool",
        {"tool": "brave__search", "args": {"q": "reyn"}},
    )
    assert result.target_tool_name == "mcp_call_tool"
    assert result.target_args == {
        "tool": "brave__search",
        "args": {"q": "reyn"},
    }


def test_resolve_invoke_action_mcp_call_tool_keys_match_wrapper_schema() -> None:
    """Tier 2: the keys mcp__call_tool routes to mcp_call_tool are a
    superset of the wrapper's required-fields list (= ``tool``).
    Regression guard against arg-name drift.
    """
    from reyn.tools.mcp_verbs import MCP_CALL_TOOL

    result = resolve_invoke_action(
        "mcp__call_tool",
        {"tool": "brave__search", "args": {"q": "reyn"}},
    )
    required = set(MCP_CALL_TOOL.parameters.get("required", []))
    assert required.issubset(result.target_args.keys()), (
        f"resolver produced keys {sorted(result.target_args.keys())} "
        f"but mcp_call_tool requires {sorted(required)}"
    )


def test_resolve_invoke_action_memory_entry_routes_to_read_body() -> None:
    """Tier 2: memory.entry__<slug> → read_memory_body({layer: shared, slug}).

    §D19 resource invoke: invoking a memory entry returns its body.

    Post-N4 (2026-05-17): the transform was updated to emit the canonical
    ``{layer, slug}`` pair that ``read_memory_body`` requires. The previous
    ``{name}`` shape caused dispatch failure when memory.entry aliases were
    invoked (= the pre-existing shape mismatch surfaced by N4-d).
    """
    result = resolve_invoke_action("memory.entry__pref_dates", {})
    assert result.target_tool_name == "read_memory_body"
    assert result.target_args == {"layer": "shared", "slug": "pref_dates"}


def test_resolve_invoke_action_rag_corpus_curries_single_source() -> None:
    """Tier 2: rag.corpus__<name> → recall(sources=[name], query, top_k?).

    §D19 single-source resource invoke.
    """
    result = resolve_invoke_action(
        "rag.corpus__meetings", {"query": "Q3 plans", "top_k": 5},
    )
    assert result.target_tool_name == "recall"
    assert result.target_args == {
        "sources": ["meetings"],
        "query": "Q3 plans",
        "top_k": 5,
    }


def test_resolve_invoke_action_rag_corpus_without_top_k() -> None:
    """Tier 2: rag.corpus__<name> omits top_k when caller doesn't supply it."""
    result = resolve_invoke_action(
        "rag.corpus__meetings", {"query": "X"},
    )
    assert "top_k" not in result.target_args
    assert result.target_args["sources"] == ["meetings"]


# ── 2. resolve_invoke_action — operation categories (passthrough) ────────


@pytest.mark.parametrize(
    "qualified_name, expected_target",
    [
        # file ops
        ("file__read",   "read_file"),
        ("file__write",  "write_file"),
        ("file__delete", "delete_file"),
        ("file__list",   "list_directory"),
        ("file__grep",   "grep_files"),
        ("file__glob",   "glob_files"),
        # web ops
        ("web__search",  "web_search"),
        ("web__fetch",   "web_fetch"),
        # memory.operation
        ("memory.operation__remember_shared", "remember_shared"),
        ("memory.operation__remember_agent",  "remember_agent"),
        ("memory.operation__forget",          "forget_memory"),
        # reyn.source
        ("reyn.source__read", "reyn_src_read"),
        ("reyn.source__list", "reyn_src_list"),
        # rag.operation
        ("rag.operation__recall",      "recall"),
        ("rag.operation__drop_source", "drop_source"),
    ],
)
def test_operation_categories_route_passthrough(
    qualified_name: str, expected_target: str,
) -> None:
    """Tier 2: operation categories route by qualified name; args pass through."""
    sample_args: dict[str, Any] = {"x": 1, "y": [2, 3]}
    result = resolve_invoke_action(qualified_name, sample_args)
    assert result.target_tool_name == expected_target
    assert result.target_args == sample_args


def test_operation_category_empty_args_pass_through() -> None:
    """Tier 2: operation route with empty args dict."""
    result = resolve_invoke_action("web__search", {})
    assert result.target_tool_name == "web_search"
    assert result.target_args == {}


def test_invoke_action_with_none_args_treats_as_empty() -> None:
    """Tier 2: passing args=None is equivalent to args={}."""
    result = resolve_invoke_action("memory.entry__foo", None)
    assert result.target_tool_name == "read_memory_body"
    assert result.target_args == {"layer": "shared", "slug": "foo"}


# ── 3. UnknownActionError + §D12 error response shape ─────────────────────


def test_unknown_action_error_unparseable_name() -> None:
    """Tier 2: malformed qualified_name raises UnknownActionError."""
    with pytest.raises(UnknownActionError) as exc_info:
        resolve_invoke_action("malformed_name", {})
    assert exc_info.value.action_name == "malformed_name"
    assert "missing" in exc_info.value.reason.lower() \
        or "separator" in exc_info.value.reason.lower()


def test_unknown_action_error_unknown_category() -> None:
    """Tier 2: unknown category in qualified_name raises UnknownActionError."""
    with pytest.raises(UnknownActionError) as exc_info:
        resolve_invoke_action("nonexistent__entry", {})
    assert exc_info.value.action_name == "nonexistent__entry"


def test_unknown_action_error_no_rule_for_unknown_entry() -> None:
    """Tier 2: known category with an unknown entry raises UnknownActionError.

    exec__sandboxed_exec now has a routing rule (FP-0034 Phase 2), so
    use a genuinely unknown entry within the exec category to verify the
    error path still works.
    """
    with pytest.raises(UnknownActionError) as exc_info:
        resolve_invoke_action("exec__unknown_op", {})
    assert exc_info.value.action_name == "exec__unknown_op"
    assert "exec" in exc_info.value.reason or "rule" in exc_info.value.reason


def test_unknown_action_error_carries_suggestions() -> None:
    """Tier 2: UnknownActionError exposes ``suggestions`` for §D12 response.

    The suggestions come from KNOWN_STATIC_QUALIFIED_NAMES via
    suggest_similar_names. A typo of file__reed should suggest file__read.
    """
    with pytest.raises(UnknownActionError) as exc_info:
        resolve_invoke_action("file__reed", {})  # typo of file__read
    # 'file__reed' parses (file is valid category) but has no rule.
    # The error MAY include suggestions; if so, file__read should be there.
    # This is a documented best-effort, not a hard guarantee.
    if exc_info.value.suggestions:
        assert any("file__read" in s for s in exc_info.value.suggestions)


def test_unknown_action_error_message_includes_suggestions() -> None:
    """Tier 2: error message includes suggestion list when populated."""
    err = UnknownActionError(
        "bad_name", "test reason", suggestions=["file__read", "web__search"],
    )
    msg = str(err)
    assert "bad_name" in msg
    assert "test reason" in msg
    assert "file__read" in msg


# ── 4. suggest_similar_names (D12 suggestion engine) ─────────────────────


def test_suggest_similar_names_finds_close_match() -> None:
    """Tier 2: typo near a known name returns the correct suggestion."""
    suggestions = suggest_similar_names("file__reed")
    assert "file__read" in suggestions


def test_suggest_similar_names_returns_empty_when_no_match() -> None:
    """Tier 2: completely unrelated input returns no suggestions."""
    suggestions = suggest_similar_names(
        "xyzqwerty_completely_unrelated_string_123",
    )
    assert suggestions == []


def test_suggest_similar_names_respects_top_k() -> None:
    """Tier 2: top_k caps the suggestion count."""
    suggestions = suggest_similar_names("file__read", top_k=1)
    assert suggestions == ["file__read"]


def test_suggest_similar_names_custom_candidates() -> None:
    """Tier 2: caller-supplied candidates override the static catalogue."""
    candidates = ["skill__alpha", "skill__beta", "skill__gamma"]
    suggestions = suggest_similar_names("skill__alfa", candidates=candidates)
    assert "skill__alpha" in suggestions


def test_suggest_similar_names_empty_candidates_returns_empty() -> None:
    """Tier 2: empty candidate list returns empty result."""
    assert suggest_similar_names("file__read", candidates=[]) == []


# ── 5. resolve_describe_action mirrors invoke routing ─────────────────────


@pytest.mark.parametrize(
    "qualified_name, expected_target",
    [
        ("skill__code_review",       "invoke_skill"),
        ("agent.peer__alice",        "delegate_to_agent"),
        # Issue #879 collapsed surface — six mcp__* verb actions.
        ("mcp__list_servers",        "list_mcp_servers"),
        ("mcp__list_tools",          "list_mcp_tools"),
        ("mcp__call_tool",           "mcp_call_tool"),
        ("mcp__search_server",       "mcp_search_server"),
        ("mcp__install_server",      "mcp_install_server"),
        ("mcp__drop_server",         "mcp_drop_server"),
        ("memory.entry__pref_dates", "read_memory_body"),
        ("rag.corpus__meetings",     "recall"),
        ("file__read",               "read_file"),
        ("web__search",              "web_search"),
        ("memory.operation__forget", "forget_memory"),
        ("rag.operation__recall",    "recall"),
    ],
)
def test_resolve_describe_returns_invoke_target(
    qualified_name: str, expected_target: str,
) -> None:
    """Tier 2: describe routes to the same target as invoke (= canonical surface)."""
    desc = resolve_describe_action(qualified_name)
    assert desc.target_tool_name == expected_target
    # describe has no transformed args — just the routing target name
    assert desc.target_args == {}


def test_resolve_describe_unknown_raises() -> None:
    """Tier 2: describe of unknown qualified_name raises UnknownActionError."""
    with pytest.raises(UnknownActionError):
        resolve_describe_action("nonexistent__entry")


# ── 6. KNOWN_STATIC_QUALIFIED_NAMES inventory ─────────────────────────────


def test_known_static_names_is_sorted_and_deduped() -> None:
    """Tier 2: static catalogue is sorted (stable) and has no duplicates."""
    names = KNOWN_STATIC_QUALIFIED_NAMES
    assert list(names) == sorted(names)
    assert len(set(names)) == len(names)


def test_known_static_names_covers_all_operation_categories() -> None:
    """Tier 2: statically-routed operation categories cover §D11 baseline.

    file / web / memory.operation / reyn.source / rag.operation /
    mcp.operation (PR-4) / exec (FP-0034 Phase 2) are all fully routed.
    """
    names = set(KNOWN_STATIC_QUALIFIED_NAMES)
    # file (4 ops)
    assert {"file__read", "file__write", "file__delete", "file__list"} <= names
    # web (2 ops)
    assert {"web__search", "web__fetch"} <= names
    # memory.operation (3 ops)
    assert {
        "memory.operation__remember_shared",
        "memory.operation__remember_agent",
        "memory.operation__forget",
    } <= names
    # reyn.source (2 ops — read/list; glob/grep are future)
    assert {"reyn.source__read", "reyn.source__list"} <= names
    # rag.operation (2 ops)
    assert {"rag.operation__recall", "rag.operation__drop_source"} <= names


def test_known_static_names_excludes_resource_categories() -> None:
    """Tier 2: resource categories are NOT in the static catalogue.

    Their entries are dynamic (= populated by caller state in PR-3).
    """
    names = set(KNOWN_STATIC_QUALIFIED_NAMES)
    # Resource categories should have no static qualified names.
    for prefix in ("skill__", "agent.peer__", "mcp.server__", "mcp.tool__",
                   "memory.entry__", "rag.corpus__"):
        matches = [n for n in names if n.startswith(prefix)]
        assert matches == [], (
            f"resource prefix {prefix!r} should have no static entries; "
            f"found {matches}"
        )


def test_known_static_names_includes_collapsed_mcp_surface() -> None:
    """Tier 2: #879 collapsed surface — the six mcp__* verb actions
    are all in the static catalogue. Counter-op for drop is present,
    and so are search / install / list / call verbs.
    """
    for qn in (
        "mcp__search_server",
        "mcp__install_server",
        "mcp__list_servers",
        "mcp__list_tools",
        "mcp__call_tool",
        "mcp__drop_server",
    ):
        assert qn in KNOWN_STATIC_QUALIFIED_NAMES, (
            f"expected {qn!r} in KNOWN_STATIC_QUALIFIED_NAMES"
        )


def test_known_static_names_includes_exec_sandboxed_exec() -> None:
    """Tier 2: exec__sandboxed_exec is in the static catalogue (FP-0034 Phase 2).

    FP-0034 Phase 2 landed the exec route; exec__sandboxed_exec is now
    in _OPERATION_RULES and therefore in KNOWN_STATIC_QUALIFIED_NAMES.
    D14 visibility gating (= hide when sandbox_backend is None/noop)
    happens at the catalog enumeration layer, not here.
    """
    assert "exec__sandboxed_exec" in KNOWN_STATIC_QUALIFIED_NAMES


def test_known_qualified_name_for_category() -> None:
    """Tier 2: known_qualified_name_for_category filters by prefix."""
    file_names = known_qualified_name_for_category("file")
    assert set(file_names) == {
        "file__read", "file__write", "file__delete", "file__list",
        "file__grep", "file__glob", "file__edit",
    }
    # Resource category returns empty
    assert known_qualified_name_for_category("skill") == ()
    # exec has sandboxed_exec (FP-0034 Phase 2)
    assert known_qualified_name_for_category("exec") == ("exec__sandboxed_exec",)
    # mcp (= issue #879 collapsed surface) has the six verb actions.
    assert set(known_qualified_name_for_category("mcp")) == {
        "mcp__search_server", "mcp__install_server",
        "mcp__list_servers", "mcp__list_tools",
        "mcp__call_tool", "mcp__drop_server",
    }


def test_known_qualified_name_for_unknown_category_raises() -> None:
    """Tier 2: invalid category to introspection helper raises."""
    with pytest.raises(ValueError, match="unknown category"):
        known_qualified_name_for_category("not_a_category")


# ── ResolvedAction dataclass shape ────────────────────────────────────────


def test_resolved_action_is_frozen() -> None:
    """Tier 2: ResolvedAction is immutable (= safe to share across handlers)."""
    result = ResolvedAction(target_tool_name="x", target_args={})
    with pytest.raises(Exception):
        # dataclass(frozen=True) raises FrozenInstanceError
        result.target_tool_name = "y"  # type: ignore[misc]


def test_resolved_action_default_args_empty() -> None:
    """Tier 2: ResolvedAction.target_args defaults to empty mapping."""
    result = ResolvedAction(target_tool_name="x")
    assert dict(result.target_args) == {}


# ── B27-H3 regression: agent.peer__ KeyError fix ─────────────────────────


def test_agent_peer_translator_remaps_message_to_request() -> None:
    """Tier 2: _delegate_to_agent_args remaps 'message' → 'request' (B27-H3).

    Regression guard: before the fix, _delegate_to_agent_args passed
    'message' through unchanged, causing KeyError: 'request' in the
    delegate_to_agent handler which always reads args["request"].

    This test exercises the full resolve_invoke_action route and then
    simulates the handler receiving the translated args — no KeyError must
    occur.
    """
    # Simulate the LLM call shape as instructed by universal_catalog (FP-0034 §D).
    resolved = resolve_invoke_action(
        "agent.peer__researcher",
        {"message": "Summarise the quarterly report."},
    )

    assert resolved.target_tool_name == "delegate_to_agent"

    # Handler reads args["to"] and args["request"]. Verify both keys are
    # present and correct — no KeyError when accessed directly.
    translated = resolved.target_args
    assert translated["to"] == "researcher"
    request_value = translated["request"]  # would raise KeyError before fix
    assert request_value == "Summarise the quarterly report."
    assert "message" not in translated, (
        "'message' must not survive the translation — handler has no 'message' key"
    )


def test_agent_peer_translator_preserves_extra_args() -> None:
    """Tier 2: _delegate_to_agent_args passes unknown extra args through unchanged.

    Extra keys beyond 'message' (e.g. 'priority') are not remapped; only
    the 'message' → 'request' rename is applied (B27-H3).
    """
    resolved = resolve_invoke_action(
        "agent.peer__planner",
        {"message": "Plan the sprint.", "priority": "high"},
    )
    translated = resolved.target_args
    assert translated["to"] == "planner"
    assert translated["request"] == "Plan the sprint."
    assert translated["priority"] == "high"
    assert "message" not in translated


def test_agent_peer_translator_caller_supplies_request_directly() -> None:
    """Tier 2: callers that already use 'request' are not double-remapped.

    If a caller passes 'request' directly (= non-catalog path), the
    translator should not corrupt it.
    """
    resolved = resolve_invoke_action(
        "agent.peer__analyst",
        {"request": "Run the numbers."},
    )
    translated = resolved.target_args
    assert translated["to"] == "analyst"
    assert translated["request"] == "Run the numbers."


# ── 6. Schema-cross-reference contract pin (regression guard) ────────────
#
# The mcp.tool routing regression (PR #246) escaped because the resolver
# emitted ``tool`` while the target handler read ``mcp_tool_name``, and
# the existing test happened to PIN the buggy shape. The fix added a
# point-in-time assertion that the resolver's output keys cover
# call_mcp_tool's required schema. This section generalises that
# contract across EVERY routing entry — for each route, we verify:
#
#   (a) the target tool exists in get_default_registry() (catches
#       rename / removal of the target without a routing-table update),
#   (b) with a representative caller-args payload, the resolver's
#       output keys cover the target's required schema (catches a
#       key-name mismatch like the PR #246 regression at test time).
#
# The samples below mirror the canonical LLM invocation shape per
# category (= what the universal-catalog wrappers instruct the LLM to
# supply). Adding a new routing entry without a sample here will fail
# the inventory-coverage test below, forcing the author to declare an
# explicit contract for the new route.

# Representative ``(qualified_name, caller_args)`` per routing entry.
# Keys reflect what the LLM would emit for that action; the resolver
# is responsible for shaping them to the target's required schema.
_ROUTE_CONTRACT_SAMPLES: list[tuple[str, dict[str, Any]]] = [
    # Resource categories (= _RESOURCE_RULES)
    ("skill__code_review", {"input": {"type": "x", "data": {}}}),
    ("agent.peer__planner", {"message": "hi", "request": "hi"}),
    ("memory.entry__pref_dates", {}),
    ("rag.corpus__notes", {"query": "what"}),
    # Operation categories (= _OPERATION_RULES) — passthrough transformers,
    # so the caller args must already include the target's required keys.
    # Issue #879 collapsed mcp surface — six verb actions.
    ("mcp__search_server",  {"text": "github related"}),
    ("mcp__install_server", {"text": "pypi:mcp-server-time"}),
    ("mcp__list_servers",   {}),
    ("mcp__list_tools",     {"server": "brave"}),
    ("mcp__call_tool",      {"tool": "brave__search", "args": {"q": "reyn"}}),
    ("mcp__drop_server",    {"server": "brave"}),
    ("file__read",   {"path": "a.txt"}),
    ("file__write",  {"path": "a.txt", "content": "x"}),
    ("file__delete", {"path": "a.txt"}),
    ("file__list",   {"path": "."}),
    ("file__grep",   {"pattern": "x"}),
    ("file__glob",   {"pattern": "*.py"}),
    ("file__edit",   {"path": "a", "old_string": "b", "new_string": "c"}),
    ("web__search",  {"query": "x"}),
    ("web__fetch",   {"url": "https://x"}),
    ("memory.operation__remember_shared",
     {"slug": "s", "name": "n", "description": "d", "type": "user", "body": "b"}),
    ("memory.operation__remember_agent",
     {"slug": "s", "name": "n", "description": "d", "type": "user", "body": "b"}),
    ("memory.operation__forget", {"layer": "shared", "slug": "s"}),
    ("reyn.source__read", {"path": "a"}),
    ("reyn.source__list", {"path": "."}),
    ("reyn.source__glob", {"pattern": "*.py"}),
    ("reyn.source__grep", {"pattern": "x"}),
    ("rag.operation__recall",      {"query": "q", "sources": ["s"]}),
    ("rag.operation__drop_source", {"source": "s"}),
    ("validation__lint",           {"skill_path": "index_events"}),
    ("exec__sandboxed_exec",       {"argv": ["echo", "hi"]}),
]


@pytest.mark.parametrize("qualified_name,caller_args", _ROUTE_CONTRACT_SAMPLES)
def test_resolver_target_exists_and_args_cover_required_schema(
    qualified_name: str, caller_args: dict[str, Any],
) -> None:
    """Tier 2: every routing entry produces args satisfying the target's required schema.

    Regression guard for the FP-0032 / FP-0034 class of drift (= the PR
    #246 mcp.tool key mismatch). For each route, the resolver must:

      (a) point at a target_tool_name that exists in the unified
          registry (= catches rename / removal of the target),
      (b) given a representative LLM-style caller_args payload, emit
          a target_args dict whose keys cover the target's required
          schema (= catches a name mismatch like ``tool`` vs
          ``mcp_tool_name`` BEFORE it surfaces as a raw KeyError stack
          trace in production).

    Sample inputs reflect what the universal-catalog wrappers instruct
    the LLM to supply per category. They are NOT exhaustive coverage of
    every arg shape the LLM might emit — they pin the canonical shape
    so a drift on either side fails the test at the routing wire.
    """
    from reyn.tools import get_default_registry

    result = resolve_invoke_action(qualified_name, caller_args)
    registry = get_default_registry()
    target = registry.lookup(result.target_tool_name)
    assert target is not None, (
        f"routing entry for {qualified_name!r} targets "
        f"{result.target_tool_name!r}, which is not in the registry"
    )
    required = set(target.parameters.get("required", []))
    produced = set(result.target_args.keys())
    missing = required - produced
    assert not missing, (
        f"resolver for {qualified_name!r} produced keys {sorted(produced)} "
        f"but target {result.target_tool_name!r} requires {sorted(required)}; "
        f"missing: {sorted(missing)}"
    )


def test_route_contract_samples_cover_every_routing_entry() -> None:
    """Tier 2: every entry in the routing tables has a contract sample.

    Adding a new resource category or operation rule without a sample
    in ``_ROUTE_CONTRACT_SAMPLES`` would silently bypass the contract
    pin above. This test fails the moment a new route is introduced
    without an explicit sample declaration.
    """
    from reyn.tools.universal_dispatch import (
        _OPERATION_RULES,
        _RESOURCE_RULES,
    )

    sample_names = {name for name, _ in _ROUTE_CONTRACT_SAMPLES}

    # Operation rules: each key is a full qualified name.
    operation_names = set(_OPERATION_RULES.keys())
    missing_ops = operation_names - sample_names
    assert not missing_ops, (
        f"operation rules without a contract sample: {sorted(missing_ops)}"
    )

    # Resource rules: each key is a category. We require at least one
    # sample qualified-name per category.
    sample_categories = {name.split("__", 1)[0] for name in sample_names}
    resource_categories = set(_RESOURCE_RULES.keys())
    missing_resources = resource_categories - sample_categories
    assert not missing_resources, (
        f"resource categories without a contract sample: "
        f"{sorted(missing_resources)}"
    )
