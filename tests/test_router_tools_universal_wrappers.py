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

import pytest

from reyn.chat.router_loop import RouterLoop
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


def test_default_flag_off_matches_explicit_false() -> None:
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


def test_flag_on_strips_legacy_and_adds_wrappers() -> None:
    """Tier 2: flag=True strips legacy per-kind tools and adds wrappers.

    Phase 6: universal_wrappers_enabled=True is now the exclusive-wrapper mode.
    Legacy per-kind tools (invoke_skill, delegate_to_agent, etc.) are stripped
    and only the universal wrappers remain as the addressing surface.
    """
    on_names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    ))
    # Wrappers present
    assert "list_actions" in on_names
    assert "describe_action" in on_names
    assert "invoke_action" in on_names
    # Legacy tools stripped
    assert "invoke_skill" not in on_names
    assert "delegate_to_agent" not in on_names


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
    but the RouterLoop dispatch set (``REGISTRY_DISPATCH_TOOLS``) was
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
        assert wrapper in RouterLoop.REGISTRY_DISPATCH_TOOLS, (
            f"Universal wrapper {wrapper!r} is in get_default_registry() "
            f"but not in RouterLoop.REGISTRY_DISPATCH_TOOLS. The LLM "
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


# ── 6. Exclusive-wrapper mode: legacy tools stripped when wrappers ON ─────
#
# FP-0034 Phase 6: hide_legacy_tools flag removed. universal_wrappers_enabled=True
# is now the unconditional exclusive-wrapper mode (legacy per-kind tools always
# stripped). Tests for the removed hide_legacy_tools kwarg are deleted.


def test_wrappers_on_strips_all_legacy_tools() -> None:
    """Tier 2: universal_wrappers_enabled=True strips all legacy per-kind tools.

    Phase 6: the hide_legacy_tools flag was removed. Stripping legacy tools
    is now unconditional when universal_wrappers_enabled=True — the LLM
    addresses everything through the universal wrappers only.
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True,
    ))
    # Wrappers present
    assert "list_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names
    # Legacy stripped unconditionally
    for legacy in (
        "list_skills", "describe_skill", "invoke_skill",
        "list_agents", "describe_agent", "delegate_to_agent",
        "list_memory", "read_memory_body",
        "read_file", "write_file", "delete_file", "list_directory",
        "web_search", "web_fetch",
        "reyn_src_list", "reyn_src_read",
    ):
        assert legacy not in names, (
            f"Legacy tool {legacy!r} must be stripped when universal_wrappers_enabled=True"
        )


# ── 6b. Universal top-level fixed tools survive exclusive-wrapper mode ────
#
# FP-0034 design (issue #36): `plan` and `ask_user` are NOT legacy tools.
# They are universal top-level fixed tools (category-外) and must remain
# visible when universal_wrappers_enabled=True.  Commit 2376e7d accidentally
# included `plan` in _LEGACY_TOOL_NAMES (B27-H1 regression).


def test_plan_present_when_wrappers_enabled() -> None:
    """Tier 2: plan is a universal top-level fixed tool, not a legacy per-kind tool.

    FP-0034 design (issue #36) explicitly designates plan as a universal
    top-level fixed tool that lives outside the category surface and must
    remain visible when universal_wrappers_enabled=True.

    Regression guard for B27-H1: plan was accidentally added to
    _LEGACY_TOOL_NAMES in commit 2376e7d, causing plan-mode to fail under
    the default config (LLM hallucinated invoke_action instead of calling plan;
    no plan_emitted events fired in 3/3 plan-mode scenarios verified by batch 27
    worker 6).

    Note: ask_user is also a universal top-level fixed tool per FP-0034 design,
    but has gates.router="deny" so it is correctly absent from build_tools output
    (which serves the router context). ask_user's absence from _LEGACY_TOOL_NAMES
    was always correct — it was never stripped, just gated at the definition level.
    """
    names = set(_tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS,
        universal_wrappers_enabled=True,
    )))
    assert "plan" in names, (
        "'plan' must be present when universal_wrappers_enabled=True "
        "(FP-0034 issue #36: plan is a universal top-level fixed tool, not a legacy tool)"
    )
    # ask_user: gated to phase-only (gates.router='deny') so correctly absent
    # from router tools. Its absence from _LEGACY_TOOL_NAMES was always correct.
    assert "ask_user" not in names, (
        "'ask_user' must NOT appear in router build_tools output — "
        "it is phase-only (gates.router='deny'). If this fails, the gate has changed."
    )


# ── 7. hot_list_aliases injected by build_tools (FP-0034 Phase 2 step 5) ──


def test_hot_list_aliases_injected_into_build_tools() -> None:
    """Tier 2: hot list aliases appear in build_tools output when universal_wrappers_enabled=True.

    FP-0034 Phase 2 step 5 contract:
      - When hot_list_aliases is passed with one or more alias dicts, each
        alias name appears in the returned tools= list.
      - Aliases appear only when universal_wrappers_enabled=True (= aliases
        are universal-catalog-adjacent and meaningless without wrappers).
      - Passing None or empty list is a no-op.
    """
    aliases = [
        {
            "type": "function",
            "function": {
                "name": "skill__foo",
                "description": "Direct alias for skill__foo. Use invoke_action for schema details.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            },
        }
    ]
    tools = build_tools(
        [], [], universal_wrappers_enabled=True, hot_list_aliases=aliases,
    )
    names = {t["function"]["name"] for t in tools}
    assert "skill__foo" in names, (
        "Hot list alias 'skill__foo' must appear in build_tools output "
        "when universal_wrappers_enabled=True and hot_list_aliases is set"
    )


def test_hot_list_aliases_absent_when_wrappers_disabled() -> None:
    """Tier 2: hot list aliases are suppressed when wrappers are off.

    Aliases are meaningless without the universal wrappers — the LLM
    would have no way to discover what a qualified_name refers to.
    build_tools must not inject them when universal_wrappers_enabled=False.
    """
    aliases = [
        {
            "type": "function",
            "function": {
                "name": "skill__bar",
                "description": "Direct alias for skill__bar.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }
    ]
    tools = build_tools(
        [], [], universal_wrappers_enabled=False, hot_list_aliases=aliases,
    )
    names = {t["function"]["name"] for t in tools}
    assert "skill__bar" not in names, (
        "Hot list alias must NOT appear when universal_wrappers_enabled=False"
    )


def test_hot_list_aliases_none_is_noop() -> None:
    """Tier 2: passing hot_list_aliases=None does not change the tools= list."""
    a = build_tools([], [], universal_wrappers_enabled=True)
    b = build_tools([], [], universal_wrappers_enabled=True, hot_list_aliases=None)
    assert a == b


# ── 8. _invoke_router_tool routes qualified names to invoke_action ────────


class _CapturingRouterLoop(RouterLoop):
    """RouterLoop subclass that records _invoke_via_registry calls.

    Overrides _invoke_via_registry to capture (name, args) without
    touching the real registry or network.  No MagicMock — pure
    subclass override per CLAUDE.md test policy.
    """

    def __init__(self) -> None:
        # Skip super().__init__ — we only need the dispatch logic,
        # not the full host/chain_id/catalog wiring.
        self.host = None  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self.calls: list[tuple[str, dict]] = []

    async def _invoke_via_registry(self, name: str, args: dict) -> Any:  # type: ignore[override]
        self.calls.append((name, args))
        return {"captured": True}


@pytest.mark.asyncio
async def test_hot_list_alias_call_redirects_to_invoke_action() -> None:
    """Tier 2: tool call with qualified-name format routes via invoke_action.

    FP-0034 Phase 2 step 5: _invoke_router_tool must forward any name
    containing '__' (= hot list alias) to _invoke_via_registry as
    invoke_action(action_name=<alias>, args=<original_args>).

    This ensures the LLM's direct alias call (e.g. skill__code_review)
    reaches the universal_dispatch handler instead of the
    'unhandled tool' error path.
    """
    loop = _CapturingRouterLoop()
    result = await loop._invoke_router_tool("skill__code_review", {"pr_url": "https://example.com"})

    assert result == {"captured": True}, "Expected _invoke_via_registry to be called"
    dispatched_name, dispatched_args = loop.calls[0]
    assert dispatched_name == "invoke_action", (
        f"Expected redirect to 'invoke_action', got {dispatched_name!r}"
    )
    assert dispatched_args == {
        "action_name": "skill__code_review",
        "args": {"pr_url": "https://example.com"},
    }, f"Unexpected invoke_action args: {dispatched_args}"


@pytest.mark.asyncio
async def test_hot_list_alias_with_none_args_uses_empty_dict() -> None:
    """Tier 2: qualified alias call with no args passes empty dict, not None.

    invoke_action handler expects args to be a dict, not None.
    The dispatch must coerce None → {} to avoid downstream KeyErrors.
    """
    loop = _CapturingRouterLoop()
    await loop._invoke_router_tool("rag.corpus__meetings", None)  # type: ignore[arg-type]

    assert loop.calls[0] == (
        "invoke_action",
        {"action_name": "rag.corpus__meetings", "args": {}},
    )


@pytest.mark.asyncio
async def test_non_qualified_name_does_not_redirect() -> None:
    """Tier 2: plain tool name without '__' falls through to unhandled error.

    Ensures the hot-list dispatch branch is not triggered for ordinary
    tool names — the caller (dispatch_tool) already validated them
    against the catalog, so unrecognised names correctly error.

    Uses a name not in REGISTRY_DISPATCH_TOOLS to avoid registry dispatch.
    The '__' check must NOT match names that are plain alphanumeric.
    """
    loop = _CapturingRouterLoop()
    # 'unknown_plain_tool' has no '__' and is not in REGISTRY_DISPATCH_TOOLS.
    result = await loop._invoke_router_tool("unknown_plain_tool", {})

    # No _invoke_via_registry call via the alias path — falls through to error
    assert loop.calls == [], "Plain name must not trigger invoke_action redirect"
    assert "error" in result
