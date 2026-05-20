"""Tier 2: catalog-partial signal + list_actions discovery-gateway shape.

2026-05-21 dogfood smoke (8 popular MCP servers) revealed that
Gemini-flash-lite (= Reyn default workhorse model) consistently
refused capability requests for installed-but-non-hot-listed MCP
tools instead of calling ``list_actions(filter='<server>')``. Root
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
    ALWAYS call list_actions(filter='<keyword>') before refusing.
    Refusing without that check is a failure mode."

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

Pins:

  1. ``_LIST_ACTIONS_DESCRIPTION`` mentions "HOT-LIST" / "subset" /
     "before refusing" — the discovery-gateway positioning.
  2. ``build_system_prompt`` output includes the catalog-partial
     signal section under ``## Action categories``.
  3. Both signals are domain-agnostic — no specific MCP server name
     or fixed token mentioned (= non-overfit per project policy).
"""
from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.tools.universal_catalog import _LIST_ACTIONS_DESCRIPTION


# ── A: SP partial-signal ─────────────────────────────────────────────


def test_sp_catalog_partial_signal_present_when_wrappers_enabled() -> None:
    """Tier 2: when universal wrappers are enabled, the SP ends the
    ``## Action categories`` section with a catalog-partial signal
    that nudges list_actions before refusing.
    """
    sp = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=True,
    )
    assert "HOT-LIST" in sp
    assert "subset" in sp
    assert "list_actions" in sp
    assert "refusing" in sp.lower() or "refuse" in sp.lower()


def test_sp_partial_signal_appears_after_action_categories() -> None:
    """Tier 2: position pin — the partial signal lives right after the
    category enumeration so the LLM reads it as a concluding rule for
    the catalog section, not a buried hint.
    """
    sp = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=True,
    )
    cat_pos = sp.find("## Action categories")
    sig_pos = sp.find("HOT-LIST")
    behav_pos = sp.find("## Behaviour")
    assert cat_pos >= 0
    assert sig_pos >= 0
    assert behav_pos >= 0
    # The signal must sit between the category section header and the
    # next major section (= Behaviour).
    assert cat_pos < sig_pos < behav_pos


def test_sp_partial_signal_absent_when_wrappers_disabled() -> None:
    """Tier 2: the signal only applies to the universal-wrappers SP
    path (= the path that exposes list_actions). When wrappers are
    off, the legacy SP doesn't reference list_actions, so the partial
    signal would be inert — omit to keep the legacy byte content
    stable for LLMReplay fixtures.
    """
    sp = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=False,
    )
    assert "HOT-LIST" not in sp


def test_sp_partial_signal_is_domain_agnostic() -> None:
    """Tier 2: the signal MUST NOT mention specific MCP server names,
    specific tools, or other domain-specific tokens. Project policy
    forbids SP overfit (= "specific server X has a tool Y" rules).
    The signal is structural — "when catalog seems incomplete, call
    list_actions" — applies uniformly to all servers / tools.
    """
    sp = build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        universal_wrappers_enabled=True,
    )
    # Extract the catalog-partial signal paragraph.
    cat_pos = sp.find("HOT-LIST")
    behav_pos = sp.find("## Behaviour")
    signal_block = sp[cat_pos:behav_pos]
    # Forbidden tokens: server names of the 8 verified MCP servers.
    forbidden = [
        "sqlite", "everything", "fetch", "memory", "git",
        "filesystem", "time", "sequential",
    ]
    for tok in forbidden:
        assert tok.lower() not in signal_block.lower(), (
            f"catalog-partial signal must not mention specific server "
            f"name {tok!r}; signal block contains it: "
            f"{signal_block[:300]!r}"
        )


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
    assert "subset" in desc.lower()
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
    """Tier 2: the new description's examples mention server-name
    filters in the abstract (e.g. "filter='sqlite'") but only as
    syntactic illustration of how the `filter` param accepts a
    string. The description must not contain a coaching rule that
    names specific MCP servers / tools as required discovery
    targets. Verify by ensuring no imperative form coaches a
    specific server lookup.
    """
    desc = _LIST_ACTIONS_DESCRIPTION
    # The description IS allowed to mention "sqlite" as a syntactic
    # example of what `filter` accepts. It must NOT contain an
    # imperative ("always check sqlite", "must call for sqlite", etc.).
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
