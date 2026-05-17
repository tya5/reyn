"""Tier 2: invoke_action ARS scope expansion (B38 D2-wrapper fix).

Contract: ``_collect_all_session_ars_entries`` collects canonical arg schemas
for ALL session-visible actions, not just hot-list actions. The scope covers:
  - All static operations (KNOWN_STATIC_QUALIFIED_NAMES) — always
  - Session skills (when skill_meta_map provided)
  - Session MCP tools (when mcp_tool_map provided)
  - Session peer agents (when available_agents provided)

B38 primary motivation: B37 N=4 same-batch observations confirmed that non-
hot-listed actions (file__write, rag.operation__drop_source, agent.peer__X)
caused LLM hallucination of non-canonical args. This test verifies the
structural contract: ARS block is hot-list-independent.

Verifies:
  A1. Registry with actions {A(schema), B(empty), C(schema)} — ARS includes
      A and C, excludes B, regardless of hot-list.
  A2. Static ops are always present (file__write, rag.operation__drop_source)
      even with no hot-list or session state.
  A3. Session skills with non-empty input_schema appear in ARS.
  A4. Skills with no input_schema do not appear (no noise).
  A5. Session MCP tools with non-empty inputSchema appear in ARS.
  A6. Session peer agents appear in ARS with the canonical peer schema.
  A7. Deduplication: if an action appears in both static ops and skill_meta_map,
      it appears only once.
  A8. ARS block label changed from "current hot-list actions" to
      "all session-visible actions" (= description reflects scope expansion).

No mocks. Uses real ToolDefinitions and real registry helpers.
"""

from __future__ import annotations

from reyn.chat.router_loop import (
    _collect_all_session_ars_entries,
    _enrich_invoke_action_description,
)
from reyn.tools import get_default_registry
from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_invoke_action_tool(description: str = "WHAT: Execute an action.") -> dict:
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


# ── A1. {A(schema), B(empty), C(schema)} pattern ─────────────────────────────


def test_ars_includes_schemed_excludes_empty_regardless_of_hotlist() -> None:
    """Tier 2: ARS includes actions with schema, excludes those without.

    Given three actions where A has {x, y}, B has empty schema, C has {z},
    verify ARS contains A and C but not B — hot-list is irrelevant.

    Uses skill_meta_map to inject synthetic actions since static ops are
    always present (and have schemas). The skill category is data-driven.
    """
    skill_meta_map = {
        "skill__alpha": {
            "description": "Alpha skill",
            "input_schema": {
                "type": "object",
                "properties": {
                    "x": {"type": "string"},
                    "y": {"type": "integer"},
                },
            },
            "input_wrapped": True,
        },
        "skill__beta": {
            "description": "Beta skill",
            # No input_schema key at all — simulates no formal schema
        },
        "skill__gamma": {
            "description": "Gamma skill",
            "input_schema": {
                "type": "object",
                "properties": {
                    "z": {"type": "string"},
                },
            },
            "input_wrapped": True,
        },
    }
    entries = _collect_all_session_ars_entries(skill_meta_map=skill_meta_map)
    entry_map = {name: props for name, props in entries}

    # A: skill__alpha with {x, y} must appear
    assert "skill__alpha" in entry_map, "skill__alpha (has schema) must be in ARS"
    assert "x" in entry_map["skill__alpha"]
    assert "y" in entry_map["skill__alpha"]

    # B: skill__beta with no input_schema must NOT appear
    assert "skill__beta" not in entry_map, "skill__beta (no schema) must not be in ARS"

    # C: skill__gamma with {z} must appear
    assert "skill__gamma" in entry_map, "skill__gamma (has schema) must be in ARS"
    assert "z" in entry_map["skill__gamma"]


# ── A2. Static ops always present ─────────────────────────────────────────────


def test_static_ops_always_present_no_session_state() -> None:
    """Tier 2: file__write and rag.operation__drop_source always appear in ARS.

    These were the B37 hot-list gap cases: they caused hallucination when
    absent from the hot list. After B38, they appear unconditionally.
    """
    entries = _collect_all_session_ars_entries()  # no session state
    entry_names = {name for name, _ in entries}

    assert "file__write" in entry_names, (
        "file__write must always be in ARS — was missing in B37 when not in hot-list"
    )
    assert "rag.operation__drop_source" in entry_names, (
        "rag.operation__drop_source must always be in ARS — B37 hallucinated source_id/source_name"
    )

    entry_map = {name: props for name, props in entries}
    assert "content" in entry_map["file__write"], (
        "file__write must expose canonical 'content' key"
    )
    assert "source" in entry_map["rag.operation__drop_source"], (
        "rag.operation__drop_source must expose canonical 'source' key"
    )


# ── A3. Session skills with input_schema appear ───────────────────────────────


def test_session_skill_with_schema_appears_in_ars() -> None:
    """Tier 2: a session skill with non-empty input_schema appears in ARS."""
    skill_meta_map = {
        "skill__code_review": {
            "description": "Reviews code",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "context": {"type": "string"},
                },
            },
            "input_wrapped": True,
        }
    }
    entries = _collect_all_session_ars_entries(skill_meta_map=skill_meta_map)
    entry_map = {name: props for name, props in entries}

    assert "skill__code_review" in entry_map
    assert "file" in entry_map["skill__code_review"]
    assert "context" in entry_map["skill__code_review"]


# ── A4. Skills without input_schema do not appear ────────────────────────────


def test_session_skill_without_schema_excluded_from_ars() -> None:
    """Tier 2: a session skill without input_schema does not contribute a line."""
    skill_meta_map = {
        "skill__no_schema": {
            "description": "No schema skill",
            "input_schema": {"type": "object", "properties": {}},  # empty properties
            "input_wrapped": True,
        }
    }
    entries = _collect_all_session_ars_entries(skill_meta_map=skill_meta_map)
    entry_names = {name for name, _ in entries}
    assert "skill__no_schema" not in entry_names, (
        "skill with empty properties must not appear in ARS"
    )


# ── A5. Session MCP tools with inputSchema appear ────────────────────────────


def test_session_mcp_tool_with_schema_appears_in_ars() -> None:
    """Tier 2: a session MCP tool with non-empty input_schema appears in ARS."""
    mcp_tool_map = {
        "mcp.tool__brave.search": {
            "description": "Brave web search",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                },
            },
        }
    }
    entries = _collect_all_session_ars_entries(mcp_tool_map=mcp_tool_map)
    entry_map = {name: props for name, props in entries}

    assert "mcp.tool__brave.search" in entry_map
    assert "query" in entry_map["mcp.tool__brave.search"]
    assert "count" in entry_map["mcp.tool__brave.search"]


# ── A6. Session peer agents appear with canonical schema ─────────────────────


def test_session_peer_agent_appears_in_ars_with_canonical_schema() -> None:
    """Tier 2: peer agents appear in ARS with the canonical delegate_to_agent schema.

    B37 W5 S3: agent.peer__researcher not in hot list → ARS block lacked
    entry → LLM followed hardcoded 'message' example → hallucinated non-canonical
    key. After B38, the peer appears in ARS with the canonical schema from
    delegate_to_agent (= {request, ...} minus the curried 'to' field).
    """
    available_agents = [
        {"name": "researcher", "role": "Research specialist"},
    ]
    entries = _collect_all_session_ars_entries(available_agents=available_agents)
    entry_map = {name: props for name, props in entries}

    # agent.peer__researcher must appear
    assert "agent.peer__researcher" in entry_map, (
        "agent.peer__researcher must appear in ARS regardless of hot-list"
    )

    # The canonical schema for peer delegation has 'request' (not 'message')
    # derived from delegate_to_agent minus the curried 'to' field.
    peer_props = entry_map["agent.peer__researcher"]
    assert "request" in peer_props, (
        "peer agent ARS must expose canonical 'request' key (not 'message')"
    )
    # The curried 'to' field must NOT appear in the ARS (it's injected by router)
    assert "to" not in peer_props, (
        "'to' is curried by the router and must not appear in the peer ARS schema"
    )


# ── A7. Deduplication ─────────────────────────────────────────────────────────


def test_deduplication_no_action_appears_twice() -> None:
    """Tier 2: each action name appears at most once in ARS entries.

    If a static op (e.g. file__write) is also present in skill_meta_map
    (which would be unusual but must not break), it appears only once.
    """
    # Inject a name that matches a static op into skill_meta_map.
    # In practice this won't happen, but the dedup guarantee must hold.
    skill_meta_map = {
        "file__write": {
            "description": "Duplicate entry",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            "input_wrapped": False,
        }
    }
    entries = _collect_all_session_ars_entries(skill_meta_map=skill_meta_map)
    names = [name for name, _ in entries]
    assert names.count("file__write") == 1, (
        "file__write must appear exactly once even if injected via skill_meta_map"
    )


# ── A8. ARS block header reflects scope expansion ────────────────────────────


def test_ars_block_header_reflects_all_session_visible_scope() -> None:
    """Tier 2: the ARS block header says 'all session-visible' not 'hot-list'.

    B38 changed the ARS block label from 'current hot-list actions' to
    'all session-visible actions'. This test verifies the scope-accurate
    label is present in the enriched description.
    """
    ars_entries = [
        ("file__write", {"path": {"type": "string"}, "content": {"type": "string"}}),
    ]
    tools = [_make_invoke_action_tool()]
    result = _enrich_invoke_action_description(tools, ars_entries)
    ia = _get_invoke_action(result)
    assert ia is not None
    desc = ia["function"]["description"]
    assert "all session-visible actions" in desc, (
        "ARS block header must say 'all session-visible actions' (B38 scope expansion)"
    )
    assert "hot-list" not in desc.lower() or "current hot-list" not in desc, (
        "ARS block header must not say 'current hot-list actions' (B37 stale label)"
    )
