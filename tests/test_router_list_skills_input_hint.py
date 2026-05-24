"""Tier 2 tests for RETRO-H2 fix (plan D): list_skills exposes input artifact + fields.

Background: B7-RETRO-H2 found that invoke_skill input field names were hallucinated
(e.g. "agent_name" instead of "target_skill" for eval_builder) because the LLM had
no structural source for field names when describe_skill was skipped.

Fix (plan D): enumerate_available_skills now reads each skill's entry phase file to
extract the input artifact name(s) and top-level field names.  _list_skills passes
these through as input_artifact and input_fields.  invoke_skill description hints at
list_skills as the first source.

All tests are pure Python, no LLM required. < 2 seconds total.

P7 constraint: no skill-specific strings appear in OS code; the hints are dynamically
extracted from DSL files, not hardcoded.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.chat.router_tools import build_tools

# ---------------------------------------------------------------------------
# Fake host / loop helpers (mirrors test_router_loop.py pattern)
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal RouterLoopHost stub for _list_skills tests."""

    def __init__(self, skills: list[dict]) -> None:
        self._skills = skills

    def list_available_skills(self) -> list[dict]:
        return list(self._skills)

    # Unused by _list_skills but required by RouterLoop constructor contract
    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self):
        return None

    def get_mcp_servers(self):
        return []

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    async def reyn_src_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_src_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}


def _make_loop(skills: list[dict]) -> RouterLoop:
    return RouterLoop(host=_FakeHost(skills), chain_id="c0")


def _get_tool_fn(tools: list[dict], name: str) -> dict | None:
    for t in tools:
        if t["function"]["name"] == name:
            return t["function"]
    return None


# ---------------------------------------------------------------------------
# (a) list_skills result includes input_artifact field when catalogue entry has it
# ---------------------------------------------------------------------------


def test_list_skills_result_includes_input_artifact():
    """Tier 2: list_skills result passes through input_artifact when the catalogue entry provides it.

    enumerate_available_skills adds input_artifact from the entry phase's input: field.
    _list_skills must propagate it to the LLM so field names are visible pre-invoke_skill.
    """
    skills = [
        {
            "name": "my_skill",
            "description": "Does something",
            "category": "general",
            "input_artifact": "my_request",
            "input_fields": ["field_a", "field_b"],
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    assert result, "result must be non-empty"
    item = result[0]
    assert item["name"] == "my_skill"
    assert item.get("input_artifact") == "my_request", (
        "input_artifact must be present in list_skills result when catalogue entry has it"
    )


# ---------------------------------------------------------------------------
# (b) list_skills result includes input_fields when catalogue entry has it
# ---------------------------------------------------------------------------


def test_list_skills_result_includes_input_fields():
    """Tier 2: list_skills result passes through input_fields (top-level field name list).

    input_fields gives the LLM the concrete field names (e.g. ["target_skill"])
    without requiring a describe_skill round-trip (RETRO-H2 root cause fix).
    """
    skills = [
        {
            "name": "my_skill",
            "description": "Does something",
            "category": "general",
            "input_artifact": "my_request",
            "input_fields": ["target_skill"],
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    item = result[0]
    assert "input_fields" in item, (
        "input_fields must be present in list_skills result when catalogue entry has it"
    )
    assert item["input_fields"] == ["target_skill"], (
        f"input_fields mismatch: expected ['target_skill'], got {item['input_fields']}"
    )


# ---------------------------------------------------------------------------
# (c) union input artifact — "|" separator preserved
# ---------------------------------------------------------------------------


def test_list_skills_union_input_artifact_separator():
    """Tier 2: list_skills result preserves the '|' separator for union input artifacts.

    When an entry phase accepts multiple artifact types (e.g. user_message |
    eval_builder_request), input_artifact must use ' | ' as a separator so the
    LLM sees the full union.
    """
    union_str = "user_message | eval_builder_request"
    skills = [
        {
            "name": "my_skill",
            "description": "Does something",
            "category": "general",
            "input_artifact": union_str,
            "input_fields": ["target_skill"],
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    item = result[0]
    assert item.get("input_artifact") == union_str, (
        f"union separator must be preserved; got {item.get('input_artifact')!r}"
    )


# ---------------------------------------------------------------------------
# (d) invoke_skill description mentions both list_skills hint and describe_skill
# ---------------------------------------------------------------------------


def test_invoke_skill_description_references_input_discovery():
    """Tier 2: invoke_skill tool description mentions input discovery options.

    The description must reference both list_skills (for the quick hint) and
    describe_skill (for full schema), so the LLM has structural guidance on
    where to find input field names without guessing.
    """
    tools = build_tools(
        [{"name": "some_skill", "description": "A skill"}],
        [],
    )
    fn = _get_tool_fn(tools, "invoke_skill")
    assert fn is not None, "invoke_skill must be present when skills are available"
    desc = fn["description"]
    assert "list_skills" in desc, (
        "invoke_skill description must mention list_skills as an input hint source"
    )
    assert "describe_skill" in desc, (
        "invoke_skill description must still mention describe_skill for full schema"
    )
    assert "input_fields" in desc or "input" in desc.lower(), (
        "invoke_skill description must reference input field discovery"
    )


# ---------------------------------------------------------------------------
# (e) safe fallback for skill with no input hint (unusual skill)
# ---------------------------------------------------------------------------


def test_list_skills_no_input_hint_is_safe():
    """Tier 2: list_skills result is safe when catalogue entry has no input hint.

    Not all skills will have input_artifact / input_fields (e.g. unusual or
    legacy skills). _list_skills must return a valid item without those fields
    rather than raising or returning None.
    """
    skills = [
        {
            "name": "bare_skill",
            "description": "No input hint",
            "category": "general",
            # no input_artifact, no input_fields
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    assert result, "result must be non-empty"
    item = result[0]
    assert item["name"] == "bare_skill"
    # Fields must be absent (not present with None value)
    assert "input_artifact" not in item, (
        "input_artifact must not appear when the catalogue entry lacks it"
    )
    assert "input_fields" not in item, (
        "input_fields must not appear when the catalogue entry lacks it"
    )


# ---------------------------------------------------------------------------
# (f) P7-clean: no skill-specific hardcodes in _skill_item
# ---------------------------------------------------------------------------


def test_skill_item_is_generic_not_skill_specific():
    """Tier 2: _skill_item forwards whatever input_artifact / input_fields the catalogue supplies.

    P7 alignment: _skill_item must work identically for any skill name.
    We verify that two different skills with different field names both appear
    correctly — the helper does not hardcode any skill-specific logic.
    """
    skills = [
        {
            "name": "alpha",
            "description": "Alpha skill",
            "category": "tools",
            "input_artifact": "alpha_request",
            "input_fields": ["query"],
        },
        {
            "name": "beta",
            "description": "Beta skill",
            "category": "tools",
            "input_artifact": "beta_input",
            "input_fields": ["document", "language"],
        },
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("tools")

    by_name = {r["name"]: r for r in result}
    assert by_name["alpha"]["input_artifact"] == "alpha_request"
    assert by_name["alpha"]["input_fields"] == ["query"]
    assert by_name["beta"]["input_artifact"] == "beta_input"
    assert by_name["beta"]["input_fields"] == ["document", "language"]


# ---------------------------------------------------------------------------
# (g) _extract_skill_input_hint real-filesystem smoke test
# ---------------------------------------------------------------------------


def test_extract_skill_input_hint_reads_stdlib_eval_builder(tmp_path: Path):
    """Tier 2: _extract_skill_input_hint correctly reads eval_builder's entry phase.

    Verifies that the helper reads a real skill directory structure and extracts
    the expected artifact name and top-level fields without raising.  Uses the
    stdlib eval_builder as a concrete fixture because RETRO-H2 was observed
    for that exact skill.

    This is a filesystem test (reads real DSL files), not an algorithm pin —
    it asserts on the public shape (input_artifact present, input_fields non-empty)
    without pinning exact field count or order.
    """
    from reyn.chat.session import _extract_skill_input_hint
    from reyn.skill.skill_paths import stdlib_root

    eval_builder_dir = stdlib_root() / "skills" / "eval_builder"
    if not eval_builder_dir.exists():
        pytest.skip("eval_builder not found in stdlib — skip real-FS test")

    hint = _extract_skill_input_hint(eval_builder_dir, "analyze_skill")

    assert "input_artifact" in hint, (
        "_extract_skill_input_hint must return input_artifact for eval_builder"
    )
    # The entry phase accepts user_message | eval_builder_request
    assert "eval_builder_request" in hint["input_artifact"], (
        "eval_builder input_artifact must include 'eval_builder_request'"
    )
    assert "input_fields" in hint, (
        "_extract_skill_input_hint must return input_fields for eval_builder"
    )
    assert isinstance(hint["input_fields"], list), "input_fields must be a list"
    assert len(hint["input_fields"]) > 0, (
        "input_fields must be non-empty for eval_builder (target_skill is required)"
    )
    assert "target_skill" in hint["input_fields"], (
        "target_skill must appear in input_fields for eval_builder (RETRO-H2 root cause)"
    )


# ---------------------------------------------------------------------------
# (h) _extract_skill_input_hint safe fallback for missing phase file
# ---------------------------------------------------------------------------


def test_extract_skill_input_hint_missing_phase_returns_empty(tmp_path: Path):
    """Tier 2: _extract_skill_input_hint returns {} gracefully when phase file is absent.

    Robustness invariant: unusual skill layouts (no phases/ dir, missing entry
    phase file) must not raise — they return an empty dict.
    """
    from reyn.chat.session import _extract_skill_input_hint

    # skill_dir with no phases/ directory
    hint = _extract_skill_input_hint(tmp_path, "nonexistent_phase")
    assert hint == {}, (
        "_extract_skill_input_hint must return {} when phase file does not exist"
    )
