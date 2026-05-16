"""Tier 2 tests for RETRO-H1+H2 fix: enum constraint on invoke_skill.name
and delegate_to_agent.to, plus flat skill list in system prompt.

All tests are pure Python, no LLM required. < 1 second total.

Background: B7-RETRO-H1 and B7-RETRO-H2 found that invoke_skill.name
had no enum constraint and the system prompt only showed category counts
(e.g. "general (10)"), not actual skill names. LLMs hallucinated skill
names like "skill_improver.review" or "eval_builder.eval_md". This fix:

  1. Injects a dynamic enum into invoke_skill.name from available_skills.
  2. Injects a dynamic enum into delegate_to_agent.to from available_agents.
  3. Injects a flat skill list + one-line descriptions into the system prompt.
"""

from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import build_tools

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SKILLS = [
    {"name": "direct_llm", "description": "Direct LLM call", "category": "general"},
    {"name": "eval", "description": "Evaluate a skill", "category": "general"},
    {"name": "skill_builder", "description": "Build a skill", "category": "general"},
]

_AGENTS = [
    {"name": "researcher", "role": "Research agent", "cluster": "default"},
    {"name": "editor", "role": "Editorial agent", "cluster": "default"},
]

_EMPTY_MEMORY: dict = {"status": "not_found", "content": ""}


def _get_tool(tools: list[dict], name: str) -> dict | None:
    """Return the function-level dict for the named tool, or None."""
    for t in tools:
        if t["function"]["name"] == name:
            return t["function"]
    return None


# ---------------------------------------------------------------------------
# (a) invoke_skill.name gets enum from available_skills
# ---------------------------------------------------------------------------


def test_invoke_skill_name_enum_matches_skill_list():
    """Tier 2: invoke_skill.name schema has enum equal to available_skills names.

    P4 alignment: the LLM can only pick from OS-provided candidates.
    """
    tools = build_tools(_SKILLS, _AGENTS)
    fn = _get_tool(tools, "invoke_skill")
    assert fn is not None, "invoke_skill tool must be present when skills are available"
    name_schema = fn["parameters"]["properties"]["name"]
    assert "enum" in name_schema, (
        "invoke_skill.name must have an enum constraint when skills are available"
    )
    assert name_schema["enum"] == ["direct_llm", "eval", "skill_builder"], (
        f"enum must match available_skills names in order; got {name_schema['enum']}"
    )


def test_invoke_skill_name_enum_preserves_order():
    """Tier 2: invoke_skill.name enum preserves the caller-supplied skill order.

    Callers control order (e.g. for priority ranking); the OS must not reorder.
    """
    skills = [
        {"name": "z_skill", "description": "last"},
        {"name": "a_skill", "description": "first"},
        {"name": "m_skill", "description": "middle"},
    ]
    tools = build_tools(skills, [])
    fn = _get_tool(tools, "invoke_skill")
    assert fn is not None
    enum = fn["parameters"]["properties"]["name"]["enum"]
    assert enum == ["z_skill", "a_skill", "m_skill"], (
        f"enum must preserve caller order; got {enum}"
    )


# ---------------------------------------------------------------------------
# (b) delegate_to_agent.to gets enum from available_agents
# ---------------------------------------------------------------------------


def test_delegate_to_agent_to_enum_matches_agent_list():
    """Tier 2: delegate_to_agent.to schema has enum equal to available_agents names.

    Same P4 alignment as invoke_skill.name — hallucinated agent names rejected.
    """
    tools = build_tools(_SKILLS, _AGENTS)
    fn = _get_tool(tools, "delegate_to_agent")
    assert fn is not None, "delegate_to_agent tool must be present"
    to_schema = fn["parameters"]["properties"]["to"]
    assert "enum" in to_schema, (
        "delegate_to_agent.to must have an enum constraint when agents are available"
    )
    assert set(to_schema["enum"]) == {"researcher", "editor"}, (
        f"to enum must match agent names; got {to_schema['enum']}"
    )


def test_delegate_to_agent_no_enum_when_empty_agents():
    """Tier 2: delegate_to_agent.to is plain string when no agents are available.

    Empty enum would be rejected by some LLM providers; omit enum entirely
    when there are no agents to constrain against.
    """
    tools = build_tools(_SKILLS, [])
    fn = _get_tool(tools, "delegate_to_agent")
    assert fn is not None, "delegate_to_agent must still appear even with no agents"
    to_schema = fn["parameters"]["properties"]["to"]
    assert "enum" not in to_schema, (
        "delegate_to_agent.to must not have an enum when agents list is empty "
        f"(got: {to_schema})"
    )
    assert to_schema.get("type") == "string", (
        "delegate_to_agent.to must remain type=string when no enum"
    )


# ---------------------------------------------------------------------------
# (c) invoke_skill omitted when available_skills=[]
# ---------------------------------------------------------------------------


def test_invoke_skill_omitted_when_no_skills():
    """Tier 2: invoke_skill is absent from the tool list when available_skills=[].

    An empty enum schema is rejected by Gemini and other providers. Omitting
    the tool entirely prevents the LLM from attempting a skill invocation
    when no skills are registered.
    """
    tools = build_tools([], _AGENTS)
    names = {t["function"]["name"] for t in tools}
    assert "invoke_skill" not in names, (
        "invoke_skill must be omitted when no skills are available "
        f"(found tools: {names})"
    )


def test_invoke_skill_present_when_skills_available():
    """Tier 2: invoke_skill is present when at least one skill is available."""
    tools = build_tools(_SKILLS, _AGENTS)
    names = {t["function"]["name"] for t in tools}
    assert "invoke_skill" in names, (
        "invoke_skill must be present when skills are available"
    )


# ---------------------------------------------------------------------------
# (d) System prompt — category-only catalog (= O(1) SP scaling)
#
# 2026-05-07 category-only retry: replaced inline `## Available skills (N)`
# enumeration with 1 pointer line "## Skills (N available) — call list_skills
# to browse". Hallucination defense is fully delegated to the schema enum
# constraint at the build_tools layer (= structural defense, see (a) above).
# Industry-aligned per Anthropic Tool Search Tool / OpenAI namespaces /
# MCP-Zero hierarchical patterns.
# ---------------------------------------------------------------------------


def test_system_prompt_does_not_inline_skill_names():
    """Tier 2: system prompt does NOT enumerate skill names inline.

    Category-only retry contract: the SP shows category-level pointer + count
    only, actual names are lazy-fetched via list_skills. Hallucination defense
    is delegated to the invoke_skill enum (see (a) tests above).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # Skill names MUST NOT appear inline — that's the whole point of category-only.
    for skill in _SKILLS:
        assert skill["name"] not in prompt, (
            f"Skill name {skill['name']!r} must NOT be inlined in SP under "
            "category-only retry; access via list_skills."
        )
    # Skill descriptions MUST NOT appear either (= they're behind list_skills /
    # describe_skill).
    assert "Direct LLM call" not in prompt, (
        "Skill description 'Direct LLM call' must NOT be inlined in SP"
    )


def test_system_prompt_uses_invoke_action_routing():
    """Tier 2: wrapper-only SP routes via invoke_action, not per-kind tools.

    Phase 6 cleanup: ## Skills section removed from SP. Discovery goes through
    list_actions(category=['skill']) at runtime via the universal catalog.
    SP uses invoke_action routing vocabulary.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # Wrapper-only path: no ## Skills section
    assert "## Skills" not in prompt
    # invoke_action is the single entry point
    assert "invoke_action" in prompt
    # ROUTING RULE (ABSOLUTE) present
    assert "ROUTING RULE (ABSOLUTE)" in prompt


def test_system_prompt_no_skills_no_skills_section():
    """Tier 2: no-skill case has no ## Skills section in wrapper-only path.

    Phase 6 cleanup: ## Skills section removed. SP is O(1) regardless of
    skill count — skills are not enumerated or section-counted in the SP.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    assert "## Skills" not in prompt
    # Basic SP structure still present
    assert "## Capabilities (routing guide)" in prompt
    assert "## Behaviour" in prompt


def test_system_prompt_size_is_o1_in_skill_count():
    """Tier 2: SP size is O(1) in the number of skills (= core scaling claim).

    With 10 skills vs 50 skills, the SP size must be identical (= category
    pointer is constant-cost). This is the headline benefit of category-only
    retry: SP scales as the catalog grows.
    """
    skills_10 = [
        {"name": f"skill_{i}", "description": f"Description {i}", "category": "general"}
        for i in range(10)
    ]
    skills_50 = [
        {"name": f"skill_{i}", "description": f"Description {i}", "category": "general"}
        for i in range(50)
    ]
    prompt_10 = build_system_prompt(
        agent_name="chat", agent_role="",
        available_skills=skills_10, available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    prompt_50 = build_system_prompt(
        agent_name="chat", agent_role="",
        available_skills=skills_50, available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # The skill count differs (10 vs 50) so the count tokens differ slightly,
    # but the structural cost should be the same modulo the count digits.
    assert abs(len(prompt_10) - len(prompt_50)) <= 5, (
        f"SP size must be O(1) in skill count; got {len(prompt_10)} (N=10) "
        f"vs {len(prompt_50)} (N=50). Difference must be ≤5 chars (= count digits only)."
    )


def test_system_prompt_legacy_ordering_no_longer_visible():
    """Tier 2: under category-only retry, ordering of skill names in SP is
    structurally moot because no individual names appear.

    The schema-layer defense (= invoke_skill enum order) still preserves
    ordering — see test_invoke_skill_name_enum_preserves_order in (a).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # No individual skill name appears inline
    assert "direct_llm" not in prompt
    assert "skill_builder" not in prompt
