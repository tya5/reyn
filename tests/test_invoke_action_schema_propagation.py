"""Tier 2: invoke_action wrapper schema propagation (B37 D2-wrapper fix).

Contract: when hot-list alias entries carry non-empty ``parameters.properties``
(= D2-min/D2-full enriched), ``_enrich_invoke_action_description`` appends a
compact per-action schema block to invoke_action's description so the LLM can
see canonical arg key names when routing via the wrapper (surface B) rather
than a direct alias (surface A).

Verifies:
  1. ``_enrich_invoke_action_description`` adds schema hint when aliases have
     non-empty properties.
  2. Hint text includes each action's canonical key names.
  3. Actions with empty properties do not contribute lines (no noise).
  4. No-op when hot_list_aliases is empty.
  5. No-op when no alias has non-empty properties.
  6. Real ToolDefinition for ``invoke_action`` is present in the default
     registry (= the same path dogfood_trace.py --mode llm-tools-schema reads).
  7. End-to-end: ``build_tools`` + ``_enrich_invoke_action_description``
     pipeline enriches invoke_action description with a registered action's
     canonical schema ({path, content}) when that action has a matching alias.

No mocks. Verifies the real ToolDefinition and the real enrichment function.
"""

from __future__ import annotations

import pytest

from reyn.chat.router_loop import _enrich_invoke_action_description
from reyn.chat.router_tools import build_tools
from reyn.tools import get_default_registry


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_alias(
    name: str,
    properties: dict | None = None,
) -> dict:
    """Build a minimal hot-list alias dict in OpenAI tools[] shape."""
    params: dict = {"type": "object"}
    if properties is not None:
        params["properties"] = properties
    else:
        params["properties"] = {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Direct alias for {name}.",
            "parameters": params,
        },
    }


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


# ── 1. Schema hint injected when aliases have non-empty properties ────────────


def test_enrich_adds_hint_when_aliases_have_schema() -> None:
    """Tier 2: hint block appears in invoke_action description after enrichment.

    Given a tools list with invoke_action and a hot-list alias with
    non-empty properties {path, content}, the enriched description must
    contain those canonical key names.
    """
    tools = [_make_invoke_action_tool()]
    aliases = [
        _make_alias("file__write", {"path": {"type": "string"}, "content": {"type": "string"}}),
    ]
    result = _enrich_invoke_action_description(tools, aliases)
    ia = _get_invoke_action(result)
    assert ia is not None, "invoke_action must still be in the tools list"
    desc = ia["function"]["description"]
    assert "file__write" in desc, "action name must appear in enriched description"
    assert "content" in desc, "canonical key 'content' must appear"
    assert "path" in desc, "canonical key 'path' must appear"


# ── 2. Hint includes all actions with non-empty properties ────────────────────


def test_enrich_includes_all_schemed_aliases() -> None:
    """Tier 2: all aliases with non-empty properties contribute a schema line."""
    tools = [_make_invoke_action_tool()]
    aliases = [
        _make_alias("file__write", {"path": {"type": "string"}, "content": {"type": "string"}}),
        _make_alias("web__search", {"query": {"type": "string"}}),
    ]
    result = _enrich_invoke_action_description(tools, aliases)
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    assert "file__write" in desc
    assert "content" in desc
    assert "web__search" in desc
    assert "query" in desc


# ── 3. Actions with empty properties do not add noise ────────────────────────


def test_enrich_skips_empty_properties_aliases() -> None:
    """Tier 2: aliases without properties (additionalProperties-only) are skipped.

    Resource aliases whose D2-full enrichment is not yet available have
    empty properties. Including them in the hint would add noise without
    giving the LLM usable key names.
    """
    tools = [_make_invoke_action_tool("ORIGINAL")]
    empty_alias = _make_alias("skill__some_skill")  # empty properties
    result = _enrich_invoke_action_description(tools, [empty_alias])
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    # No hint block should be appended — description is original
    assert "ORIGINAL" in desc
    assert "skill__some_skill" not in desc


# ── 4. No-op when hot_list_aliases is empty ──────────────────────────────────


def test_enrich_noop_empty_aliases() -> None:
    """Tier 2: empty alias list returns tools unchanged."""
    tools = [_make_invoke_action_tool("UNCHANGED")]
    result = _enrich_invoke_action_description(tools, [])
    assert result is tools, "empty alias list must return the same list object"


# ── 5. No-op when no alias has non-empty properties ──────────────────────────


def test_enrich_noop_all_empty_properties() -> None:
    """Tier 2: when all aliases have empty properties, tools list returned unchanged."""
    tools = [_make_invoke_action_tool("UNCHANGED")]
    aliases = [_make_alias("skill__foo"), _make_alias("skill__bar")]
    result = _enrich_invoke_action_description(tools, aliases)
    ia = _get_invoke_action(result)
    assert ia is not None
    assert ia["function"]["description"] == "UNCHANGED"


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

    Given a registered action with schema {path, content} (= file__write),
    when build_tools is called with universal_wrappers_enabled=True and then
    _enrich_invoke_action_description is called with a matching alias, the
    LLM-visible invoke_action description includes those canonical key names.

    This mirrors the exact RouterLoop code path:
      tools = build_tools(..., universal_wrappers_enabled=True, hot_list_aliases=aliases)
      tools = _enrich_invoke_action_description(tools, aliases)
    """
    # Use a minimal file__write alias with canonical {path, content} schema
    # (same schema as the real write_file ToolDefinition)
    aliases = [
        _make_alias(
            "file__write",
            {"path": {"type": "string"}, "content": {"type": "string"}},
        )
    ]
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
    tools = _enrich_invoke_action_description(tools, aliases)
    ia_after = _get_invoke_action(tools)
    assert ia_after is not None, "invoke_action must still be in tools after enrichment"
    desc_after = ia_after["function"]["description"]

    # The enriched description must carry the canonical key names
    assert "file__write" in desc_after, "file__write must appear in enriched description"
    assert "content" in desc_after, "canonical key 'content' must appear in enriched description"
    assert "path" in desc_after, "canonical key 'path' must appear in enriched description"
    # The original description content is preserved
    assert desc_before[:50] in desc_after, "original description prefix must be preserved"
