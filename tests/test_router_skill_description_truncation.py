"""Tier 2 tests for G12 attractor mitigation: skill description truncation.

Background (B7 finding — a62a9dad / a947255e):
  Empty-stop attractor root cause = skill description verbosity (specifically
  skill_improver's 218-char description).  Truncating to ≤80 chars in BOTH
  trigger paths reduced empty-stop from 100% → 0% (H-b verification).

Two trigger paths addressed in B7 wave:
  Pattern A: list_skills tool_response (router calls list_skills then attractor)
  Pattern C: system prompt inline skill list (router stops before list_skills)

One additional trigger path addressed in B11-R2 wave:
  Pattern D: describe_skill tool_response verbosity (routing field triggers P-b
  attractor — 1000+ chars in describe response).  Fix: strip routing + category
  fields from describe_skill response.  B11-R2 N-shot result: 20% → 0%.

Fix: MAX_DESC_LEN_FOR_LISTING = 80 applied in _skill_item (Pattern A) and
build_system_prompt inline skill list (Pattern C).  describe_skill now strips
routing + category fields (Pattern D) — see _DESCRIBE_SKILL_STRIP_FIELDS.

All tests are pure Python, no LLM required. < 2 seconds total.
"""
from __future__ import annotations

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import _DESCRIBE_SKILL_STRIP_FIELDS, MAX_DESC_LEN_FOR_LISTING

# ---------------------------------------------------------------------------
# Helpers / fake host (mirrors test_router_list_skills_input_hint.py pattern)
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal RouterLoopHost stub for _list_skills / _describe_skill tests."""

    def __init__(self, skills: list[dict]) -> None:
        self._skills = skills

    def list_available_skills(self) -> list[dict]:
        return list(self._skills)

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

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}


def _make_loop(skills: list[dict]) -> RouterLoop:
    return RouterLoop(host=_FakeHost(skills), chain_id="c0")


_LONG_DESC = "A" * 120  # 120 chars — well over MAX_DESC_LEN_FOR_LISTING (80)
_SHORT_DESC = "Short desc"  # 10 chars — well under limit
_EXACT_DESC = "E" * 80  # exactly at limit — must NOT be truncated


# ---------------------------------------------------------------------------
# (a) list_skills tool_response description is ≤ MAX_DESC_LEN_FOR_LISTING
#     for long descriptions (Pattern A mitigation)
# ---------------------------------------------------------------------------


def test_list_skills_long_description_truncated():
    """Tier 2: list_skills result truncates descriptions longer than MAX_DESC_LEN_FOR_LISTING.

    Pattern A trigger path: router calls list_skills; verbose description in
    tool_response triggers G12 empty-stop attractor (B7 finding a62a9dad).
    After fix, description in list_skills result must be ≤ MAX_DESC_LEN_FOR_LISTING.
    """
    skills = [
        {
            "name": "verbose_skill",
            "description": _LONG_DESC,
            "category": "general",
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    assert len(result) == 1
    item = result[0]
    assert len(item["description"]) <= MAX_DESC_LEN_FOR_LISTING + 3, (
        f"list_skills description must be ≤ {MAX_DESC_LEN_FOR_LISTING} chars + '...' "
        f"(got {len(item['description'])} chars)"
    )


# ---------------------------------------------------------------------------
# (b) Truncation format: first 80 chars + "..."
# ---------------------------------------------------------------------------


def test_list_skills_truncation_appends_ellipsis():
    """Tier 2: truncated descriptions end with '...' (80 chars + ellipsis = 83 total).

    The truncation format is: desc[:MAX_DESC_LEN_FOR_LISTING] + "..."
    This makes truncation visible to the LLM (summary signal vs full content).
    """
    skills = [
        {
            "name": "long_skill",
            "description": _LONG_DESC,
            "category": "general",
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    item = result[0]
    assert item["description"].endswith("..."), (
        "Truncated description must end with '...'"
    )
    # First MAX_DESC_LEN_FOR_LISTING chars must be preserved
    assert item["description"][: MAX_DESC_LEN_FOR_LISTING] == _LONG_DESC[: MAX_DESC_LEN_FOR_LISTING], (
        "First MAX_DESC_LEN_FOR_LISTING chars must be preserved verbatim"
    )


# ---------------------------------------------------------------------------
# (c) Short descriptions are left untouched (no spurious ellipsis)
# ---------------------------------------------------------------------------


def test_list_skills_short_description_untouched():
    """Tier 2: descriptions ≤ MAX_DESC_LEN_FOR_LISTING are returned verbatim.

    Truncation must only fire when description exceeds the threshold.
    Short or exact-length descriptions must not get ellipsis appended.
    """
    for desc in [_SHORT_DESC, _EXACT_DESC]:
        skills = [{"name": "short_skill", "description": desc, "category": "general"}]
        loop = _make_loop(skills)
        result = loop._list_skills("general")

        item = result[0]
        assert item["description"] == desc, (
            f"Description of {len(desc)} chars must be returned verbatim "
            f"(MAX_DESC_LEN_FOR_LISTING={MAX_DESC_LEN_FOR_LISTING}); "
            f"got {item['description']!r}"
        )


# ---------------------------------------------------------------------------
# (d) System prompt inline skill list also truncates (Pattern C mitigation)
# ---------------------------------------------------------------------------


def test_system_prompt_long_description_truncated():
    """Tier 2: build_system_prompt truncates skill descriptions in the inline skill list.

    Pattern C trigger path: router does NOT call list_skills; description verbosity
    in the system prompt's inline skill list triggers the empty-stop attractor
    (B7 finding a947255e).  After fix, description in the prompt must be truncated.
    """
    skills = [
        {
            "name": "verbose_skill",
            "description": _LONG_DESC,
            "category": "general",
        }
    ]
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=skills,
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
    )

    # The full long description must NOT appear in the prompt
    assert _LONG_DESC not in prompt, (
        "Full long description must not appear in system prompt (truncation expected)"
    )
    # The truncated prefix MUST appear (with ellipsis)
    expected_truncated = _LONG_DESC[:MAX_DESC_LEN_FOR_LISTING] + "..."
    assert expected_truncated in prompt, (
        f"Truncated description ({expected_truncated!r}) must appear in system prompt"
    )


def test_system_prompt_short_description_untouched():
    """Tier 2: build_system_prompt leaves short descriptions verbatim in the inline list."""
    skills = [
        {
            "name": "short_skill",
            "description": _SHORT_DESC,
            "category": "general",
        }
    ]
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=skills,
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
    )

    # Short description appears verbatim (no spurious ellipsis)
    assert f"short_skill: {_SHORT_DESC}" in prompt, (
        f"Short description must appear verbatim in system prompt; "
        f"checked for 'short_skill: {_SHORT_DESC}'"
    )


# ---------------------------------------------------------------------------
# (e) describe_skill: description preserved, routing+category stripped (B11-R2)
# ---------------------------------------------------------------------------


def test_describe_skill_returns_full_description():
    """Tier 2: describe_skill preserves name/description/input fields verbatim.

    describe_skill is the invocation-guidance path.  Description text is NOT
    truncated (unlike list_skills).  The routing + category fields ARE stripped
    to prevent the P-b verbosity attractor (B11-R2: Pattern D fix).
    """
    skills = [
        {
            "name": "long_skill",
            "description": _LONG_DESC,
            "category": "general",
        }
    ]
    loop = _make_loop(skills)
    result = loop._describe_skill("long_skill")

    # Description must be returned verbatim (no truncation)
    assert result.get("description") == _LONG_DESC, (
        "describe_skill must return the full untruncated description "
        f"(expected {len(_LONG_DESC)} chars, got {len(result.get('description', ''))})"
    )
    assert "..." not in result.get("description", ""), (
        "describe_skill description must not contain truncation ellipsis"
    )


def test_describe_skill_strips_routing_and_category():
    """Tier 2: describe_skill strips routing and category fields (Pattern D fix).

    B11-R2 finding: describe_skill routing field (~1000 chars) triggers the G12
    P-b verbosity attractor (20% empty-stop rate).  Stripping routing + category
    reduces response to ~200 chars and eliminates the attractor (0/10).

    All fields in _DESCRIBE_SKILL_STRIP_FIELDS must be absent from the result.
    The invocation-critical fields (name, description, input_artifact,
    input_fields) must be preserved.
    """
    skills = [
        {
            "name": "skill_with_routing",
            "description": "A test skill.",
            "category": "general",
            "routing": {
                "intents": ["task"],
                "when_to_use": ["When the user asks to do X"],
                "when_not_to_use": ["When the user wants Y"],
                "examples": {"positive": ["Do X"], "negative": ["Do Y"]},
            },
            "input_artifact": "user_message",
            "input_fields": ["field_a"],
        }
    ]
    loop = _make_loop(skills)
    result = loop._describe_skill("skill_with_routing")

    # Stripped fields must be absent
    for field in _DESCRIBE_SKILL_STRIP_FIELDS:
        assert field not in result, (
            f"describe_skill must strip the '{field}' field "
            f"(_DESCRIBE_SKILL_STRIP_FIELDS: {_DESCRIBE_SKILL_STRIP_FIELDS})"
        )

    # Invocation-critical fields must be present
    assert result.get("name") == "skill_with_routing", "name must be preserved"
    assert result.get("description") == "A test skill.", "description must be preserved"
    assert result.get("input_artifact") == "user_message", "input_artifact must be preserved"
    assert result.get("input_fields") == ["field_a"], "input_fields must be preserved"


def test_describe_skill_strip_fields_constant():
    """Tier 2: _DESCRIBE_SKILL_STRIP_FIELDS contains the expected fields (B11-R2).

    This test pins the constant value so accidental changes to the strip set
    are caught.  The set must include at minimum 'routing' and 'category'.
    """
    assert "routing" in _DESCRIBE_SKILL_STRIP_FIELDS, (
        "_DESCRIBE_SKILL_STRIP_FIELDS must contain 'routing' (P-b verbosity trigger)"
    )
    assert "category" in _DESCRIBE_SKILL_STRIP_FIELDS, (
        "_DESCRIBE_SKILL_STRIP_FIELDS must contain 'category' (internal metadata)"
    )


# ---------------------------------------------------------------------------
# (f) Backward compat: list_skills output preserves name / input_artifact /
#     input_fields fields (truncation affects description content only)
# ---------------------------------------------------------------------------


def test_list_skills_backward_compat_fields_preserved():
    """Tier 2: list_skills item still has name, input_artifact, input_fields after truncation.

    The truncation change must not alter the output structure — only the
    description content value changes.  Backward compat: callers that consume
    name / input_artifact / input_fields must not break.
    """
    skills = [
        {
            "name": "structured_skill",
            "description": _LONG_DESC,
            "category": "general",
            "input_artifact": "my_request",
            "input_fields": ["field_x", "field_y"],
        }
    ]
    loop = _make_loop(skills)
    result = loop._list_skills("general")

    assert len(result) == 1
    item = result[0]
    assert item["name"] == "structured_skill", "name field must be preserved"
    assert item.get("input_artifact") == "my_request", (
        "input_artifact field must be preserved after truncation"
    )
    assert item.get("input_fields") == ["field_x", "field_y"], (
        "input_fields field must be preserved after truncation"
    )


# ---------------------------------------------------------------------------
# (g) All stdlib skills: truncated description ≤ MAX_DESC_LEN_FOR_LISTING
# ---------------------------------------------------------------------------


def test_all_stdlib_skills_description_within_limit():
    """Tier 2: all stdlib skills produce truncated descriptions within the limit.

    Verifies that enumerate_available_skills + _list_skills together produce
    descriptions that never exceed MAX_DESC_LEN_FOR_LISTING for every stdlib
    skill.  Skips if the stdlib path cannot be resolved (CI isolation).
    """
    try:
        from reyn.chat.session import enumerate_available_skills
    except ImportError:
        pytest.skip("enumerate_available_skills not importable — skip stdlib test")

    try:
        stdlib_skills = enumerate_available_skills(exclude=set())
    except Exception as exc:
        pytest.skip(f"enumerate_available_skills raised {exc!r} — skip")

    if not stdlib_skills:
        pytest.skip("No stdlib skills found — skip")

    loop = _make_loop(stdlib_skills)
    # Drill into each category to get skill items
    categories = set()
    for s in stdlib_skills:
        categories.add(s.get("category") or "general")

    all_items: list[dict] = []
    for cat in categories:
        all_items.extend(loop._list_skills(cat))

    violations: list[str] = []
    for item in all_items:
        desc = item.get("description", "")
        if len(desc) > MAX_DESC_LEN_FOR_LISTING + 3:  # +3 for "..."
            violations.append(
                f"{item['name']!r}: {len(desc)} chars — {desc[:50]!r}..."
            )

    assert not violations, (
        f"These skills produced descriptions exceeding the limit "
        f"({MAX_DESC_LEN_FOR_LISTING} + '...'):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# (h) MAX_DESC_LEN_FOR_LISTING constant value matches finding doc recommendation
# ---------------------------------------------------------------------------


def test_max_desc_len_constant_is_80():
    """Tier 2: MAX_DESC_LEN_FOR_LISTING equals 80 (B7 finding recommendation).

    B7-G12-context-root-cause.md (a62a9dad) confirmed that truncating to ≤80
    chars reduces empty-stop rate from 100% → 0%.  This test pins the constant
    value to the finding-confirmed threshold so accidental changes are caught.
    """
    assert MAX_DESC_LEN_FOR_LISTING == 80, (
        f"MAX_DESC_LEN_FOR_LISTING must be 80 (B7 finding threshold); "
        f"got {MAX_DESC_LEN_FOR_LISTING}"
    )
