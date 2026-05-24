"""Tier 2: invoke_action wrapper schema propagation (B38 scope expansion).

Contract: ``_enrich_invoke_action_description`` accepts a list of
``(qualified_name, properties_dict)`` tuples (ARS entries) and appends a
compact ACTION ARG SCHEMAS block to invoke_action's description. The scope
is all session-visible actions (not just hot-list), produced by
``_collect_all_session_ars_entries``.

B37 contract change: the function now accepts ``list[tuple[str, dict]]``
instead of ``list[dict]`` (hot-list aliases). Tests updated to match the
B38 API.

Verifies:
  1. ``_enrich_invoke_action_description`` adds schema hint when ARS entries
     carry non-empty properties.
  2. Hint text includes each action's canonical key names.
  3. Entries with empty properties dict do not contribute lines (no noise).
  4. No-op when ars_entries is empty.
  5. No-op when all entries have empty properties.
  6. Real ToolDefinition for ``invoke_action`` is present in the default
     registry (= the same path dogfood_trace.py --mode llm-tools-schema reads).
  7. End-to-end: ``build_tools`` + ``_enrich_invoke_action_description``
     pipeline enriches invoke_action description with static operations'
     canonical schemas regardless of hot-list state.
  8. ``_collect_all_session_ars_entries`` includes all static ops from
     KNOWN_STATIC_QUALIFIED_NAMES (not just hot-list).

No mocks. Verifies real ToolDefinitions and real enrichment functions.
"""

from __future__ import annotations

from reyn.chat.router_loop import (
    _collect_all_session_ars_entries,
    _enrich_invoke_action_description,
)
from reyn.chat.router_tools import build_tools
from reyn.tools import get_default_registry
from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_invoke_action_tool(description: str = "WHAT: Execute an action.") -> dict:
    """Build a minimal invoke_action entry in OpenAI tools[] shape."""
    return {
        "type": "function",
        "function": {
            "name": "invoke_action",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action_name": {"type": "string"},
                    "args": {"type": "object"},
                },
                "required": ["action_name"],
            },
        },
    }


def _get_invoke_action(tools: list[dict]) -> dict | None:
    for t in tools:
        if (t.get("function") or {}).get("name") == "invoke_action":
            return t
    return None


# ── 1. Schema hint injected when ARS entries have non-empty properties ─────────


def test_enrich_adds_hint_when_entries_have_schema() -> None:
    """Tier 2: hint block appears in invoke_action description after enrichment.

    Given a tools list with invoke_action and ARS entries with non-empty
    properties {path, content}, the enriched description must contain those
    canonical key names.
    """
    tools = [_make_invoke_action_tool()]
    ars_entries = [
        ("file__write", {"path": {"type": "string"}, "content": {"type": "string"}}),
    ]
    result = _enrich_invoke_action_description(tools, ars_entries)
    ia = _get_invoke_action(result)
    assert ia is not None, "invoke_action must still be in the tools list"
    desc = ia["function"]["description"]
    assert "file__write" in desc, "action name must appear in enriched description"
    assert "content" in desc, "canonical key 'content' must appear"
    assert "path" in desc, "canonical key 'path' must appear"


# ── 2. Hint includes all entries with non-empty properties ───────────────────


def test_enrich_includes_all_schemed_entries() -> None:
    """Tier 2: all ARS entries with non-empty properties contribute a schema line."""
    tools = [_make_invoke_action_tool()]
    ars_entries = [
        ("file__write", {"path": {"type": "string"}, "content": {"type": "string"}}),
        ("web__search", {"query": {"type": "string"}}),
    ]
    result = _enrich_invoke_action_description(tools, ars_entries)
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    assert "file__write" in desc
    assert "content" in desc
    assert "web__search" in desc
    assert "query" in desc


# ── 3. Entries with empty properties render as ``{}`` (B40) ─────────────────


def test_enrich_renders_empty_properties_entries_as_empty_braces() -> None:
    """Tier 2: B40 — ARS entries with empty properties render as ``<name>: {}``.

    Empty-schema actions (= e.g. ``skill__mcp_search`` without an explicit
    ``input_schema`` artifact) must surface in the ARS catalog so wrapper-path
    routing can pick them by name. Rendering as ``{}`` signals "exists, no
    formal arg schema" without falsely implying the action takes no args.

    Care-boundary §1: the LLM doesn't have to guess what exists.
    Reverses the pre-B40 "skip empty-props" contract; B40 cognitive-bias fix
    (B39 W6 R-WEB: 0/10 → 10/10 mcp_search routing recovery via trace-patch-replay).
    """
    tools = [_make_invoke_action_tool("ORIGINAL")]
    ars_entries = [("skill__some_skill", {})]  # empty properties
    result = _enrich_invoke_action_description(tools, ars_entries)
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    assert "ORIGINAL" in desc
    assert "skill__some_skill: {}" in desc


# ── 4. No-op when ars_entries is empty ──────────────────────────────────────


def test_enrich_noop_empty_entries() -> None:
    """Tier 2: empty ARS entry list returns tools unchanged."""
    tools = [_make_invoke_action_tool("UNCHANGED")]
    result = _enrich_invoke_action_description(tools, [])
    assert result is tools, "empty entry list must return the same list object"


# ── 5. All-empty-properties entries still emit ARS block (B40) ──────────────


def test_enrich_emits_block_for_all_empty_properties_entries() -> None:
    """Tier 2: B40 — ARS block is emitted even when every entry has empty props.

    Skill-discovery context: an LLM that sees ``skill__foo: {}`` /
    ``skill__bar: {}`` in the catalog can pick them by name and use
    ``describe_action`` to resolve args. Skipping the block entirely (= old
    contract) would hide their existence and force category-prefix guessing.
    """
    tools = [_make_invoke_action_tool("ORIGINAL")]
    ars_entries = [("skill__foo", {}), ("skill__bar", {})]
    result = _enrich_invoke_action_description(tools, ars_entries)
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    assert "ORIGINAL" in desc
    assert "skill__foo: {}" in desc
    assert "skill__bar: {}" in desc


# ── 6. Real ToolDefinition for invoke_action in default registry ──────────────


def test_invoke_action_real_tool_definition_in_registry() -> None:
    """Tier 2: invoke_action ToolDefinition is retrievable from the default registry.

    This is the same path dogfood_trace.py --mode llm-tools-schema reads.
    Verifies the real tool is registered and has the expected top-level schema.
    """
    registry = get_default_registry()
    tool_def = registry.lookup("invoke_action")
    assert tool_def is not None, "invoke_action must be in the default registry"
    assert tool_def.name == "invoke_action"
    assert isinstance(tool_def.parameters, dict)
    # The static schema has action_name and args properties
    props = (tool_def.parameters.get("properties") or {})
    assert "action_name" in props, "invoke_action must have action_name parameter"
    assert "args" in props, "invoke_action must have args parameter"
    # Critically: args has no properties (= the schema gap D2-wrapper fixes)
    args_schema = props["args"]
    assert "properties" not in args_schema or not args_schema.get("properties"), (
        "invoke_action.args must not carry per-action properties in the static schema "
        "(the gap this fix addresses is in the description layer)"
    )


# ── 7. End-to-end: build_tools pipeline enriches invoke_action description ───


def test_e2e_build_tools_invoke_action_description_enriched() -> None:
    """Tier 2: build_tools + _enrich_invoke_action_description pipeline.

    With universal wrappers enabled and no session state (no skills, agents,
    MCP tools), the ARS block still includes all static ops from
    KNOWN_STATIC_QUALIFIED_NAMES — including file__write {content, path}.
    This mirrors the B38 scope expansion: schema is always available
    regardless of hot-list state.
    """
    ars_entries = _collect_all_session_ars_entries()
    tools = build_tools(
        [],  # no skills needed
        [],  # no agents needed
        universal_wrappers_enabled=True,
    )
    # Confirm invoke_action is present before enrichment
    ia_before = _get_invoke_action(tools)
    assert ia_before is not None, "invoke_action must be in tools with wrappers enabled"
    desc_before = ia_before["function"]["description"]
    assert "file__write" not in desc_before, (
        "file__write must NOT be in description before enrichment"
    )

    # Apply enrichment
    tools = _enrich_invoke_action_description(tools, ars_entries)
    ia_after = _get_invoke_action(tools)
    assert ia_after is not None, "invoke_action must still be in tools after enrichment"
    desc_after = ia_after["function"]["description"]

    # The enriched description must carry canonical key names for static ops
    assert "file__write" in desc_after, "file__write must appear in enriched description"
    assert "content" in desc_after, "canonical key 'content' must appear in enriched description"
    assert "path" in desc_after, "canonical key 'path' must appear in enriched description"
    # The original description content is preserved
    assert desc_before[:50] in desc_after, "original description prefix must be preserved"


# ── 8. _collect_all_session_ars_entries includes all static ops ───────────────


def test_collect_all_session_ars_entries_includes_static_ops() -> None:
    """Tier 2: _collect_all_session_ars_entries covers all static operation actions.

    Without any session state, the returned entries must include every
    KNOWN_STATIC_QUALIFIED_NAME that has a non-empty schema. This is the
    B38 scope expansion contract: ARS is no longer hot-list-scoped.
    """
    entries = _collect_all_session_ars_entries()
    entry_names = {name for name, _ in entries}

    # Every static op with a non-empty schema must be present.
    # B37 bug: file__write, rag.operation__drop_source absent from ARS when
    # not in hot list. After B38, they must always be present.
    assert "file__write" in entry_names, "file__write must always be in ARS (B37 bug fixed)"
    assert "rag.operation__drop_source" in entry_names, (
        "rag.operation__drop_source must always be in ARS (B37 bug fixed)"
    )
    assert "web__search" in entry_names, "web__search must be in ARS"
    assert "file__read" in entry_names, "file__read must be in ARS"

    # file__write must have canonical {content, path} keys
    file_write_props = next(
        (props for name, props in entries if name == "file__write"), None
    )
    assert file_write_props is not None
    assert "content" in file_write_props, "file__write must expose canonical 'content' key"
    assert "path" in file_write_props, "file__write must expose canonical 'path' key"

    # rag.operation__drop_source must have canonical {source} key
    drop_source_props = next(
        (props for name, props in entries if name == "rag.operation__drop_source"), None
    )
    assert drop_source_props is not None
    assert "source" in drop_source_props, (
        "rag.operation__drop_source must expose canonical 'source' key (not source_id/source_name)"
    )


# ── 9. B40: empty-schema skills surface in ARS via known_skill_names ─────────


class TestCollectArsEntriesEmptySchemaSkills:
    """Tier 2: B40 cognitive-bias fix — empty-schema skills must surface in ARS.

    Pre-B40 contract: ``_collect_all_session_ars_entries`` only emitted
    actions with non-empty properties. Empty-schema skills (e.g.
    ``skill__mcp_search`` without an explicit ``input_schema`` artifact)
    were invisible to wrapper-path routing, causing cold-start cognitive
    bias (B39 W6 R-WEB: ``"mcp_search スキル"`` → 100% mcp.server
    miscategorization at hot_list_n=10 + fresh action_usage state).

    Post-B40 contract: when ``known_skill_names`` is provided, every
    qualified name in that set appears in the ARS — schemed skills with
    their property dict, empty-schema skills with ``{}``. Wrapper-path
    routing now has full visibility of session skills regardless of
    schema availability. Care-boundary §1: "the LLM doesn't have to
    guess what exists."
    """

    def test_known_skill_names_none_preserves_pre_b40_behavior(self) -> None:
        """Tier 2: without known_skill_names, empty-schema skills stay excluded."""
        entries = _collect_all_session_ars_entries(
            skill_meta_map={
                "skill__alpha": {
                    "input_schema": {"properties": {"q": {"type": "string"}}}
                },
            },
        )
        names = {n for n, _ in entries}
        # schemed skill included via Source 2
        assert "skill__alpha" in names
        # no empty-schema skill present (= no Source 2b inputs)
        for n, props in entries:
            if n.startswith("skill__"):
                assert props, (
                    f"pre-B40 path must not emit empty-props skill entries, "
                    f"got ({n!r}, {props!r})"
                )

    def test_known_skill_names_surfaces_empty_schema_skills(self) -> None:
        """Tier 2: known_skill_names with empty-schema entries emits ``{}`` props."""
        entries = _collect_all_session_ars_entries(
            skill_meta_map={
                "skill__alpha": {
                    "input_schema": {"properties": {"q": {"type": "string"}}}
                },
            },
            known_skill_names=frozenset({
                "skill__alpha",        # schemed — already in skill_meta_map
                "skill__mcp_search",   # empty-schema — must appear via Source 2b
                "skill__word_stats_demo",  # empty-schema — must appear via Source 2b
            }),
        )
        entries_by_name = dict(entries)
        # schemed skill keeps its properties (= not overwritten by Source 2b)
        assert entries_by_name["skill__alpha"] == {"q": {"type": "string"}}
        # empty-schema skills appear with empty dict
        assert entries_by_name["skill__mcp_search"] == {}
        assert entries_by_name["skill__word_stats_demo"] == {}

    def test_known_skill_names_does_not_duplicate(self) -> None:
        """Tier 2: Source 2 entries are not re-emitted by Source 2b."""
        entries = _collect_all_session_ars_entries(
            skill_meta_map={
                "skill__alpha": {
                    "input_schema": {"properties": {"q": {"type": "string"}}}
                },
            },
            known_skill_names=frozenset({"skill__alpha"}),
        )
        alpha_entries = [e for e in entries if e[0] == "skill__alpha"]
        (only_entry,) = alpha_entries
        # The single entry retains its full properties (Source 2 wins over 2b)
        assert only_entry[1] == {"q": {"type": "string"}}

    def test_known_skill_names_empty_frozenset_is_noop(self) -> None:
        """Tier 2: empty known_skill_names produces no Source 2b entries."""
        entries_baseline = _collect_all_session_ars_entries()
        entries_with_empty = _collect_all_session_ars_entries(
            known_skill_names=frozenset(),
        )
        assert {n for n, _ in entries_baseline} == {n for n, _ in entries_with_empty}
