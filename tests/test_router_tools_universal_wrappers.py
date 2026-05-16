"""Tier 2: FP-0034 PR-3b-i universal_wrappers_enabled flag in build_tools.

Verifies the new opt-in flag that appends the 4 universal catalog
wrappers (list_actions / describe_action / invoke_action;
search_actions deferred to PR-3b-ii) at the END of the tools= list.

Contract:
  - universal_wrappers_enabled=False (default) → byte-identical to prior
    build_tools output (no new tools appended).
  - universal_wrappers_enabled=True → existing tools unchanged + the
    3 wrappers appended in canonical order (= list_actions,
    describe_action, invoke_action).
  - search_actions is NOT included even when flag=True (= PR-3b-ii adds
    it once embedding gating lands).

No mocks. No LLMReplay (= PR-3b-iii re-records fixtures when the
default flips). Pure list-of-dicts contract tests on the build_tools
return value.
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
    """Tier 2: flag=True appends list_actions / describe_action /
    invoke_action in §D21 canonical order at the end of tools=.

    search_actions is intentionally absent in PR-3b-i (= D14 embedding
    gating lands in PR-3b-ii).
    """
    names = _tool_names(build_tools(
        _SAMPLE_SKILLS, _SAMPLE_AGENTS, universal_wrappers_enabled=True,
    ))
    # All 3 wrappers present
    assert "list_actions" in names
    assert "describe_action" in names
    assert "invoke_action" in names
    # search_actions still absent (PR-3b-ii territory)
    assert "search_actions" not in names

    # Canonical relative order: list_actions < describe_action < invoke_action
    assert names.index("list_actions") < names.index("describe_action")
    assert names.index("describe_action") < names.index("invoke_action")


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
