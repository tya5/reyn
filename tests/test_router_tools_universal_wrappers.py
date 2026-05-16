"""Tier 2: FP-0034 universal_wrappers_enabled flag in build_tools.

Verifies the flag-gated 3 universal catalog wrappers (list_actions /
describe_action / invoke_action) appended at the END of the tools=
list. ``search_actions`` is deferred to Phase 2 — visibility (§D14
embedding gate) + handler (ActionEmbeddingIndex) land together.

Contract:
  - universal_wrappers_enabled=False (= direct callers / fixture-safe
    path) → byte-identical to prior build_tools output (no new tools
    appended).
  - universal_wrappers_enabled=True → existing tools unchanged + the
    3 wrappers appended in canonical order (= list_actions,
    describe_action, invoke_action).
  - search_actions is NOT included even when flag=True (Phase 2).

No mocks. No LLMReplay. Pure list-of-dicts contract tests on the
build_tools return value.
"""

from __future__ import annotations

from typing import Any

from reyn.chat.router_tools import build_tools


_SAMPLE_SKILLS = [{"name": "example_skill", "description": "An example skill"}]
_SAMPLE_AGENTS = [{"name": "peer_agent", "role": "Peer"}]


def _tool_names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


# ── 1. Default (flag off) preserves existing tools= shape ────────────────


def test_default_flag_off_excludes_universal_wrappers() -> None:
    """Tier 2: with the default (False), no universal wrappers appear."""
    tools = build_tools(_SAMPLE_SKILLS, _SAMPLE_AGENTS)
    names = set(_tool_names(tools))
    for w in ("list_actions", "search_actions", "describe_action",
              "invoke_action"):
        assert w not in names, (
            f"{w!r} must NOT appear when universal_wrappers_enabled defaults "
            f"to False (= PR-3b-i preserves prior tools= byte shape)"
        )


def test_default_flag_off_byte_identical_to_explicit_false() -> None:
    """Tier 2: default flag matches explicit False.

    Defensive check — confirms no unintentional default flip during
    refactor.  Both calls must return identical tool name sequences.
    """
    a = _tool_names(build_tools(_SAMPLE_SKILLS, _SAMPLE_AGENTS))
    b = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=False,
    ))
    assert a == b


# ── 2. Flag on appends the 3 wrappers in canonical order ─────────────────


def test_flag_on_appends_three_wrappers_in_order() -> None:
    """Tier 2: flag=True (without search visibility) appends 3 wrappers.

    search_actions stays out of tools= when ``search_actions_visible``
    is False (= no embedding configured / index not ready), per §D14.
    Phase 2 step 1 lifts the absolute exclusion: when the visibility
    flag is True, search_actions appears (see
    ``test_flag_on_with_search_visible_appends_four_wrappers``).
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    ))
    # All 3 wrappers present (search_actions gated separately)
    assert "list_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names
    assert "search_actions" not in names

    # Canonical relative order: list_actions < describe_action < invoke_action
    assert names.index("list_actions") < names.index("describe_action")
    assert names.index("describe_action") < names.index("invoke_action")


def test_flag_on_with_search_visible_appends_four_wrappers() -> None:
    """Tier 2: §D14 visibility gate — search_actions joins when visible.

    Phase 2 step 1: when ``search_actions_visible=True`` (= operator
    configured embedding_class AND ActionEmbeddingIndex.is_ready()),
    build_tools includes the 4th wrapper at the appropriate position
    in the canonical order.
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True,
        search_actions_visible=True,
    ))
    # All 4 wrappers present
    assert "list_actions" in names
    assert "search_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names

    # Canonical order: list < search < describe < invoke
    assert names.index("list_actions") < names.index("search_actions")
    assert names.index("search_actions") < names.index("describe_action")
    assert names.index("describe_action") < names.index("invoke_action")


def test_search_visible_alone_does_not_inject_wrappers() -> None:
    """Tier 2: search_actions_visible=True without wrappers=True is a no-op.

    Safety: the search visibility gate is meaningful ONLY when the
    universal wrappers are themselves enabled.  Setting
    ``search_actions_visible=True`` while ``universal_wrappers_enabled``
    stays False keeps the legacy tools= shape unchanged.
    """
    a = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=False,
        search_actions_visible=True,
    ))
    b = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=False,
    ))
    assert a == b
    assert "search_actions" not in a


def test_flag_on_wrappers_at_end_of_tools_list() -> None:
    """Tier 2: wrappers append AT THE END so the existing cache prefix
    is preserved when the flag flips on for the first time.

    A cache prefix is everything BEFORE the first ephemeral marker;
    appending at the end ensures all prior tools stay byte-stable
    in their previous positions.
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    ))
    # The last 3 entries should be the wrappers in canonical order
    assert names[-3:] == ["list_actions", "describe_action", "invoke_action"]


def test_flag_on_existing_tools_unchanged() -> None:
    """Tier 2: flag=True adds wrappers WITHOUT removing or reordering
    any existing tool from the flag=False output."""
    base_names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=False,
    ))
    on_names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    ))
    # Every base tool appears in the enabled list at the same index
    for i, name in enumerate(base_names):
        assert on_names[i] == name, (
            f"flag=True must preserve existing tools order; base[{i}]={name!r}"
            f" but enabled[{i}]={on_names[i]!r}"
        )
    # Length difference = exactly 3 (PR-3b-i wrappers; search_actions excluded)
    assert len(on_names) - len(base_names) == 3


# ── 3. Wrapper schemas pass OpenAI tool[] contract ───────────────────────


def test_flag_on_wrapper_shapes_are_openai_function_tools() -> None:
    """Tier 2: each appended wrapper is an OpenAI tool[] entry.

    Contract:
      - top-level ``type == "function"``
      - nested ``function.name`` matches the wrapper name
      - nested ``function.description`` non-empty string
      - nested ``function.parameters.type == "object"``
    """
    tools = build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    )
    wrappers = [
        t for t in tools
        if t["function"]["name"] in (
            "list_actions", "describe_action", "invoke_action",
        )
    ]
    assert len(wrappers) == 3
    for t in wrappers:
        assert t["type"] == "function"
        func = t["function"]
        assert isinstance(func["description"], str) and func["description"]
        params = func["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


# ── 4. get_dispatch_kind resolves wrapper names via registry ─────────────


def test_get_dispatch_kind_resolves_universal_wrappers() -> None:
    """Tier 2: get_dispatch_kind returns 'sync' for the 3 wrappers.

    Wrappers ship with dispatch_kind='sync' (= default) in the
    ToolDefinition; the registry resolution must surface that.
    """
    from reyn.chat.router_tools import get_dispatch_kind
    for w in ("list_actions", "describe_action", "invoke_action"):
        assert get_dispatch_kind(w) == "sync"


# ── 5. Wrapper inclusion is independent of file/mcp/agent state ──────────


def test_wrappers_are_in_router_loop_dispatch_set() -> None:
    """Tier 2: drift invariant — the 4 wrappers are wired for dispatch.

    Prevents the regression that landed initially in PR-3b-i: wrappers
    were added to ``tools=`` and registered in the unified registry,
    but the RouterLoop dispatch set (``_REGISTRY_DISPATCH_TOOLS``) was
    not extended, so when the LLM actually called list_actions /
    describe_action / invoke_action the router returned
    ``{"error": "unhandled tool: <name>"}`` to the LLM.

    This invariant ensures any future wrapper added to the universal
    catalog also lands in the dispatch set, or the next maintainer
    sees this test fail immediately.
    """
    from reyn.chat.router_loop import RouterLoop
    for wrapper in (
        "list_actions", "search_actions",
        "describe_action", "invoke_action",
    ):
        assert wrapper in RouterLoop._REGISTRY_DISPATCH_TOOLS, (
            f"Universal wrapper {wrapper!r} is in get_default_registry() "
            f"but not in RouterLoop._REGISTRY_DISPATCH_TOOLS. The LLM "
            f"would see 'unhandled tool: {wrapper}' on every call."
        )


def test_flag_on_wrappers_present_even_with_empty_skills_agents() -> None:
    """Tier 2: wrappers appear with empty skill / agent lists.

    Wrappers are universal (§D21 category-external) so they don't
    depend on skill / agent / MCP / file presence.
    """
    names = _tool_names(build_tools(
        [], [], universal_wrappers_enabled=True,
    ))
    assert "list_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names


# ── 6. hide_legacy_tools exclusive-wrapper mode (FP-0034 Phase 2 prep) ────


def test_hide_legacy_alone_is_noop() -> None:
    """Tier 2: hide_legacy_tools=True without wrappers is a no-op.

    Safety guard: if the operator hides legacy tools but doesn't also
    enable wrappers, the LLM would have no addressing surface at all.
    build_tools refuses to strip legacy in that misconfiguration —
    the additive (legacy-only) shape stays.
    """
    a = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=False, hide_legacy_tools=True,
    ))
    b = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=False, hide_legacy_tools=False,
    ))
    assert a == b, (
        "hide_legacy_tools=True without wrappers must not strip "
        "legacy — the LLM would have no addressing surface left"
    )
    # Legacy tools still present
    assert "invoke_skill" in a
    assert "delegate_to_agent" in a


def test_hide_legacy_with_wrappers_strips_legacy() -> None:
    """Tier 2: wrappers ON + hide_legacy_tools ON → only wrappers visible.

    Phase 2 exclusive-wrapper mode: the LLM sees only list_actions /
    describe_action / invoke_action and addresses everything through
    qualified names.  The legacy per-kind surface is gone.
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True, hide_legacy_tools=True,
    ))
    # Wrappers present
    assert "list_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names
    # All legacy stripped
    for legacy in (
        "list_skills", "describe_skill", "invoke_skill",
        "list_agents", "describe_agent", "delegate_to_agent",
        "list_memory", "read_memory_body",
        "remember_shared", "remember_agent", "forget_memory",
        "read_file", "write_file", "delete_file", "list_directory",
        "web_search", "web_fetch",
        "reyn_src_list", "reyn_src_read",
        "plan",
    ):
        assert legacy not in names, (
            f"Legacy tool {legacy!r} must be stripped when "
            f"hide_legacy_tools=True + wrappers ON"
        )


def test_hide_legacy_default_off_preserves_coexistence() -> None:
    """Tier 2: default hide_legacy_tools=False keeps additive shape.

    Default behavior of Phase 1: wrappers ON + legacy ON simultaneously.
    The LLM can pick either path; this is the steady state until
    production-confirmed.
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True,
        # hide_legacy_tools omitted → default False
    ))
    # Wrappers AND legacy both present
    assert "list_actions" in names
    assert "invoke_skill" in names


def test_hide_legacy_byte_identical_to_explicit_false() -> None:
    """Tier 2: omitting kwarg matches explicit False."""
    a = build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True,
    )
    b = build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True, hide_legacy_tools=False,
    )
    assert a == b
