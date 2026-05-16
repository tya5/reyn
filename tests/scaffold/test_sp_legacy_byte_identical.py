"""Scaffold test: SP legacy-path byte-identity (FP-0034 B23-PRE-1 → Phase 5).

# scaffold: triggered_by="FP-0034 Phase 5 — hide_legacy_tools default flip to True"
# scaffold: removed_by="The PR that lands the Phase 5 default flip"

Bounded-life test guarding LLMReplay fixture validity during the FP-0034
Phase 4 preview window: while `hide_legacy_tools` is opt-in (default False),
the SP rendered with the default must remain byte-identical to the legacy
SP (= pre-B23-PRE-1 wording), so the 7 fixtures under
``tests/fixtures/llm/router/`` keep their SHA-256 keys valid and 0
re-records are required.

Once Phase 5 lands and the default flips to True, byte-identity with the
legacy SP is no longer the contract — the new default IS the wrapper-only
SP, and fixtures get re-recorded as part of that PR. At that point this
scaffold test is obsolete and the PR that fires the trigger event must
remove this file (= per testing.ja.md Annex discipline).
"""
from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt

_BASE_KWARGS: dict = dict(
    agent_name="default",
    agent_role="generalist",
    available_skills=[{"name": "code_review", "description": "review code"}],
    available_agents=[{"name": "alice", "role": "helper"}],
    memory_index={"status": "ok", "shared": [], "agent": []},
    file_permissions={"read": ["*"], "write": ["*"]},
    mcp_servers=[],
    universal_wrappers_enabled=True,
)


def test_default_false_is_byte_identical_to_legacy() -> None:
    """Scaffold: default hide_legacy_tools=False must be byte-identical to legacy.

    Guarantees that 7 existing LLMReplay fixtures under tests/fixtures/llm/router/
    keep their SHA-256 keys valid (= 0 re-records required) during the Phase 4
    preview window. Removed when Phase 5 flips the default to True.
    """
    sp_default = build_system_prompt(**_BASE_KWARGS)
    sp_explicit_false = build_system_prompt(
        **_BASE_KWARGS, hide_legacy_tools=False,
    )
    assert sp_default == sp_explicit_false
