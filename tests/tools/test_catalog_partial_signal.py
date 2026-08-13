"""Tier 2: catalog-partial signal + list_actions discovery-gateway shape.

2026-05-21 dogfood smoke (8 popular MCP servers) revealed that
Gemini-flash-lite (= Reyn default workhorse model) consistently
refused capability requests for installed-but-non-hot-listed MCP
tools instead of calling ``list_actions`` to discover them. Root
cause analysis (= per-trace replay across sqlite / everything /
fetch):

  - The hot-list shown to the LLM is a subset (= 20 actions seeded
    + usage-tracked).
  - ``list_actions`` description framed it as "for known-action
    lookup", not "for capability discovery when catalog is partial".
  - SP did not signal that the catalog was partial.

→ LLM concluded "this list = complete inventory" + refused.

Two structural fixes landed in this PR (no domain-specific SP
coaching):

  - (A) ``router_system_prompt.py`` ``## Action categories``
    section now ends with an explicit catalog-partial signal:
    "function list is a HOT-LIST (= a subset). Whenever the user
    requests a capability and no listed tool obviously matches,
    ALWAYS call list_actions before refusing. Refusing without
    that check is a failure mode."

  - (B) ``_LIST_ACTIONS_DESCRIPTION`` rewritten from "browse the
    catalog" to "discover the FULL catalog superset". The "WHEN"
    clause now says: "use this whenever the user requests a
    capability and you do not see a directly-named tool for it".

Trace-replay measurement (N=20 against sqlite "list tables" prompt):

  - Pre-fix: 0% list_actions calls (= 100% inline refuse)
  - Post-fix: 45% list_actions calls (= 55% still refuse)

Improvement is real (= 0 → 45%) but not complete; the remaining
55% refusal is LLM-side stochastic caution that this PR does not
attempt to override. A future structural follow-up (= seed
``mcp.server__<name>`` entries directly into the hot-list so the
LLM sees server existence without discovery) will be evaluated after
A+B production data is collected.

#4552: the hot-list feature described above (fix (A), the SP-side
HOT-LIST/subset paragraph, and its "subset" framing in fix (B)'s
description) was removed entirely, owner directive. The tests pinning
fix (A) and the "subset" wording of fix (B) were removed with it; the
remaining discovery-gateway-shape assertions of fix (B) (FULL catalog /
before-refusing / failure-mode framing, POST_CALL directive,
domain-agnosticism) are unaffected and still pinned below.

Pins:

  1. ``_LIST_ACTIONS_DESCRIPTION`` mentions "FULL catalog" /
     "before refusing" — the discovery-gateway positioning.
  2. ``build_system_prompt`` output still renders ``## Action
     categories`` with its list_actions discovery vocabulary.
  3. The description is domain-agnostic — no specific MCP server name
     or fixed token mentioned (= non-overfit per project policy).
"""
from __future__ import annotations

from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.universal_catalog import _LIST_ACTIONS_DESCRIPTION


def _slots(*, universal_wrappers_enabled: bool = True) -> "dict[str, str]":
    """Build slot-map for catalog-partial signal tests."""
    return build_universal_tool_use_slots(
        universal_wrappers_enabled=universal_wrappers_enabled,
        search_actions_enabled=False,
        discovery_mandate=False,
        non_interactive=False,
    )


# ── A: SP partial-signal ─────────────────────────────────────────────
#
# #4552: the hot-list feature (and its HOT-LIST subset-signal paragraph,
# HOT_LIST_ALIASES_HINT) was removed entirely from
# src/reyn/prompt/universal_slots.py. The tests that pinned that
# paragraph's presence/absence (with-aliases signal text, no-aliases
# omission, the with/without distinctness regression pin, its position
# relative to ## Action categories, its absence when wrappers are off,
# and its domain-agnostic wording) tested a behavior that no longer
# exists and were deleted rather than kwarg-patched.


def test_sp_no_aliases_action_categories_section_still_present() -> None:
    """Tier 2: ## Action categories section is still rendered with its
    list_actions discovery vocabulary when universal wrappers are enabled.
    #1627 Stage 4: slot-map via build_universal_tool_use_slots.
    """
    sp = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_agents=[],
        memory_index={},
        tool_use_sp=_slots(),
    )
    assert "## Action categories" in sp
    assert "list_actions" in sp


# ── B: list_actions discovery-gateway description ────────────────────


def test_list_actions_description_positions_as_discovery_gateway() -> None:
    """Tier 2: ``_LIST_ACTIONS_DESCRIPTION`` frames the tool as the
    gateway to a catalog SUPERSET, not a utility for known-action
    lookup. Pin the key phrasing so a future copy-edit doesn't
    silently revert to the "browse the catalog" framing that the
    pre-fix LLM ignored.
    """
    desc = _LIST_ACTIONS_DESCRIPTION
    # Discovery positioning.
    assert "FULL catalog" in desc or "full catalog" in desc.lower()
    # Anti-refuse directive.
    assert "before refusing" in desc.lower() or "BEFORE refusing" in desc
    # Failure-mode framing for the missing-check case.
    assert "failure mode" in desc.lower()


def test_list_actions_description_keeps_post_call_directive() -> None:
    """Tier 2: the existing POST_CALL directive ("after list_actions
    reveals matching action, MUST follow with describe_action or
    invoke_action") must remain — this PR ADDS pre-call discovery
    framing, not replace the post-call rule.
    """
    desc = _LIST_ACTIONS_DESCRIPTION
    assert "POST_CALL" in desc
    assert "describe_action" in desc and "invoke_action" in desc


def test_list_actions_description_is_domain_agnostic() -> None:
    """Tier 2: the description must not contain a coaching rule that
    names specific MCP servers / tools as required discovery
    targets. Project policy forbids SP overfit (= "specific server X
    has a tool Y" rules). The rule is structural — "when catalog
    seems incomplete, call list_actions" — applies uniformly to all
    servers / tools. Verify by ensuring no imperative form coaches
    a specific server lookup.
    """
    desc = _LIST_ACTIONS_DESCRIPTION
    # The description must NOT contain an imperative that names a
    # specific server (= "always check sqlite", "must call for
    # sqlite", "specifically for the sqlite server", etc.).
    forbidden_imperatives = [
        "always call for sqlite",
        "must check sqlite",
        "specifically for the sqlite server",
    ]
    desc_lower = desc.lower()
    for phrase in forbidden_imperatives:
        assert phrase not in desc_lower, (
            f"description contains domain-specific imperative: {phrase!r}"
        )
