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

import pytest

from reyn.chat.router_tools import build_tools
from reyn.chat.router_system_prompt import build_system_prompt


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
# (d) System prompt has flat skill list injected
# ---------------------------------------------------------------------------


def test_system_prompt_contains_flat_skill_list():
    """Tier 2: build_system_prompt injects a flat list of skill names.

    The LLM must see actual skill names (not just category counts) to avoid
    zero-shot hallucination of skill names (RETRO-H1+H2 root cause).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # All three skill names must appear
    for skill in _SKILLS:
        assert skill["name"] in prompt, (
            f"Skill name '{skill['name']}' must appear in system prompt "
            f"(flat list injection missing)"
        )


def test_system_prompt_contains_skill_descriptions():
    """Tier 2: flat skill list includes one-line descriptions alongside names.

    Descriptions give the LLM context to judge skill relevance, reducing
    the attractor risk (RETRO-H1 post-PR37 regression was caused by showing
    names without context).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    # At least one description must appear alongside the name
    assert "Direct LLM call" in prompt, (
        "Skill description 'Direct LLM call' must appear in system prompt "
        "(flat list must include descriptions)"
    )


def test_system_prompt_flat_list_available_skills_section():
    """Tier 2: system prompt has an 'Available skills' section header."""
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    assert "Available skills" in prompt, (
        "System prompt must have an 'Available skills' section (RETRO-H1+H2 fix)"
    )


def test_system_prompt_no_skills_shows_none():
    """Tier 2: flat list shows '(none)' when no skills available."""
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    assert "(none)" in prompt, (
        "System prompt must indicate '(none)' when no skills are available"
    )


# ---------------------------------------------------------------------------
# (e) Flat list preserves available_skills ordering, at least partially
# ---------------------------------------------------------------------------


def test_system_prompt_flat_list_contains_multiple_skills():
    """Tier 2: flat list contains all skills from available_skills (>= 3 here).

    A loose assertion: we don't pin exact line format, but we verify that
    all three skill names appear in the section — not just category counts.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    names_found = [s["name"] for s in _SKILLS if s["name"] in prompt]
    assert len(names_found) >= 3, (
        f"Expected all 3 skill names in prompt; found: {names_found}"
    )


def test_system_prompt_flat_list_first_skill_before_last():
    """Tier 2: flat list preserves ordering — first skill appears before last.

    We don't pin exact line numbers (algorithm-level), but we assert ordering
    is preserved at the string level: direct_llm appears before skill_builder.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
    )
    idx_first = prompt.index("direct_llm")
    idx_last = prompt.index("skill_builder")
    assert idx_first < idx_last, (
        "Flat skill list must preserve caller-supplied order: "
        f"direct_llm (pos {idx_first}) should appear before "
        f"skill_builder (pos {idx_last})"
    )
