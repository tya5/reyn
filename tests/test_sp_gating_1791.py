"""Tier 2: OS-invariant tests for the #1791 SP-improvement gating contracts.

Pins the design-judgment adoptions from #1791 and — critically — their
**non-harm gating**: each addition must NOT change the SP for the contexts it is
gated against.

Invariants:
  (a) A1 TASK_COMPLETION (anti-fabrication / finish-the-task) is in the static
      Behaviour core for EVERY agent (all-model, memory or not).
  (b) #3 memory-quality guidance renders ONLY when the memory tool is active
      (memory_index status "ok"); absent otherwise.
  (c) A2 model-family hygiene is in slot_in_behaviour ONLY for non-Claude models;
      a Claude model's slot is unchanged (the non-harm gate).
  (d) model_family() classifies the resolved model string coarsely.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / patch. Real public-surface calls.
- No private-state assertions; no count-pins; behavioral / membership assertions.
- Each test docstring first line is exactly ``Tier 2: ...``.
"""
from __future__ import annotations

from reyn.llm.model_resolver import model_family
from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_A1_MARK = "Finishing the job"          # A1 TASK_COMPLETION signature phrase
_M3_MARK = "Memory guidance"            # #3 memory-quality signature phrase
_A2_MARK = "Verify before acting"       # A2 model-family signature phrase

_BASE = dict(agent_name="t", agent_role="", available_skills=[], available_agents=[])


def _slots(non_claude: bool) -> dict:
    return build_universal_tool_use_slots(
        universal_wrappers_enabled=True, search_actions_enabled=True,
        discovery_mandate=False, has_hot_list_aliases=False, non_claude=non_claude,
    )


# (a) A1 — always present (all-model, memory-independent) -------------------------

def test_a1_task_completion_always_in_static_core() -> None:
    """Tier 2: A1 TASK_COMPLETION is in the static Behaviour core regardless of memory."""
    sp_mem = build_system_prompt(memory_index={"status": "ok", "content": "x"}, **_BASE)
    sp_nomem = build_system_prompt(memory_index={"status": "not_found", "content": ""}, **_BASE)
    assert _A1_MARK in sp_mem and _A1_MARK in sp_nomem, (
        "A1 (anti-fabrication / finish-the-task) must be all-model static-core, "
        "present whether or not the memory tool is active"
    )


# (b) #3 — memory-gated -----------------------------------------------------------

def test_memory_guidance_present_only_with_memory_tool() -> None:
    """Tier 2: #3 memory-quality guidance renders iff the memory tool is active."""
    sp_mem = build_system_prompt(memory_index={"status": "ok", "content": "x"}, **_BASE)
    sp_nomem = build_system_prompt(memory_index={"status": "not_found", "content": ""}, **_BASE)
    assert _M3_MARK in sp_mem, "#3 must render when memory_index status is 'ok'"
    assert _M3_MARK not in sp_nomem, (
        "#3 must NOT render when the memory tool is inactive (non-harm gate — "
        "no memory-guidance cost on non-memory agents)"
    )


# (c) A2 — non-Claude gated (the non-harm gate) -----------------------------------

def test_a2_model_family_hygiene_non_claude_only() -> None:
    """Tier 2: A2 model-family hygiene is in slot_in_behaviour for non-Claude only."""
    nc = _slots(non_claude=True)
    cl = _slots(non_claude=False)
    assert _A2_MARK in nc.get("slot_in_behaviour", ""), (
        "A2 (verify/dep-check/concise) must be present for non-Claude models"
    )
    assert _A2_MARK not in cl.get("slot_in_behaviour", ""), (
        "A2 must be ABSENT for Claude — the non-harm gate (Claude SP unchanged)"
    )


# (d) model_family classifier -----------------------------------------------------

def test_model_family_classifier_coarse() -> None:
    """Tier 2: model_family() coarsely classifies the resolved model string."""
    assert model_family("openai/gemini-2.5-flash-lite") == "gemini"
    assert model_family("claude-opus-4") == "claude"
    assert model_family("anthropic/claude-3-5-sonnet") == "claude"
    assert model_family("openai/gpt-5") == "gpt"
    assert model_family("qwen3-coder") == "other"
