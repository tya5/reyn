"""HN follow-up Q4/Q6/Q8: regression nets for tool descriptions + system prompt.

These tests pin the *structural presence* of attractor-mitigation phrases
landed for HN dogfood follow-up (Q4 list_skills empty-stop, Q6 README path
discovery, Q8 project_context priority over web_search). They do NOT pin
exact wording — they only check that the mitigation phrase / convention
hint is still present, so that future edits do not silently drop it.

Tier 2 per docs/ja/contributing/testing.md (OS invariant: tool /
system-prompt builders expose the documented mitigation surface).
"""

from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import build_tools


def _find_tool(tools: list[dict], name: str) -> dict:
    for t in tools:
        if t.get("function", {}).get("name") == name:
            return t["function"]
    raise AssertionError(f"tool {name} not found in build_tools output")


def test_q4_list_skills_description_directs_narration() -> None:
    """Tier 2: list_skills description tells the LLM to narrate names directly.

    Q4 mitigation — without this hint, the LLM stops after the tool returns
    instead of repeating the skill names back to the user (G12 empty-stop
    attractor variant).
    """
    tools = build_tools(
        available_skills=[{"name": "demo", "description": "x"}],
        available_agents=[],
    )
    desc = _find_tool(tools, "list_skills")["description"].lower()

    # Structural assertion: the description must steer the LLM toward
    # narrating skill names (not stopping silently).
    assert "narrate" in desc, (
        f"list_skills description lost narration directive: {desc!r}"
    )
    assert "skill names" in desc or "names directly" in desc, (
        f"list_skills description lost 'skill names' phrasing: {desc!r}"
    )


def test_q6_read_file_description_has_root_convention_hint() -> None:
    """Tier 2: read_file description carries project-root convention hints.

    Q6 mitigation — without these hints, the LLM asks the user where
    README/CLAUDE.md/etc. live instead of trying the conventional
    project-root path directly.
    """
    tools = build_tools(
        available_skills=[],
        available_agents=[],
        file_permissions={"read": ["."]},
    )
    desc = _find_tool(tools, "read_file")["description"]

    # Structural assertion: README + project root must both be mentioned
    # so the LLM can guess `./README.md` without asking.
    assert "README.md" in desc, (
        f"read_file description lost README.md hint: {desc!r}"
    )
    assert "project root" in desc.lower(), (
        f"read_file description lost project-root hint: {desc!r}"
    )


def test_q8_system_prompt_prefers_project_context_over_web_search() -> None:
    """Tier 2: system prompt declares project_context as primary, web_search as supplementary.

    Q8 mitigation — without this directive, the LLM proposes web_search
    even when project_context already contains the answer (e.g. comparing
    Reyn to LangGraph).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="general-purpose router",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        project_context="Reyn is an Agent OS for predictable workflows.",
    )

    lower = prompt.lower()
    # Both pieces of the directive must coexist somewhere in the prompt.
    assert "project_context" in lower and "primary" in lower, (
        "system prompt missing project_context-as-primary phrasing"
    )
    assert "web_search" in lower and "supplementary" in lower, (
        "system prompt missing web_search-as-supplementary phrasing"
    )


def test_q8_directive_only_when_project_context_present() -> None:
    """Tier 2: priority directive is gated on non-empty project_context.

    When project_context is empty, the directive would refer to nothing —
    so it must not be emitted (avoids dangling "above" reference).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="general-purpose router",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        project_context="",
    )

    lower = prompt.lower()
    # The "primary source" directive must NOT appear when project_context
    # is empty — otherwise it points at nothing.
    assert "project_context" not in lower or "primary" not in lower, (
        "system prompt emitted project_context-priority directive without "
        "project_context content"
    )
