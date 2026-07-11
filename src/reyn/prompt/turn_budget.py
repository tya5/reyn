"""§F — the turn-budget service's wrap-up system prompt.

Feeds ``reyn.services.turn_budget.engine`` (``TurnBudgetEngine`` — measured
once at engine init as ``T_wrap_SP`` — and the force-close call sent through
``RouterLoop``). The prompt is Axis-independent (P7-clean) and field-agnostic
(P8-clean): it switches the model's role to "consolidate and stop" without
naming any skill, phase, or artifact type, or describing the Control IR
output schema — that stays the OS's job at the calling seam. Sibling of
``compaction.py``'s (§E) summariser SPs.

The ``reason`` variant is NOT a second static string — it is the SAME
``WRAP_UP_SYSTEM_PROMPT`` with a caller-supplied cause line prepended at call
time (``reason=None`` is the cumulative-axis path and keeps the prompt
cause-neutral). The parameterized function that performs that prepend is
what moves here (mirrors ``router_frame.py``'s ``cwd_reference_mapping_sentence``
prefix+function split).
"""
from __future__ import annotations

# WHEN: always — the sole system prompt the turn-budget force-close call
#       sends (via `wrap_up_system_prompt()`), whether cumulative-axis
#       (reason=None) or reason-tagged.
# WHERE: reyn.services.turn_budget.engine.TurnBudgetEngine (T_wrap_SP
#        measurement) and the force-close call routed through RouterLoop.
# WHY: #1092 §8 — one SP that asks the model to consolidate what's done, where
#      outputs live, what remains, and what must not be repeated, so a fresh
#      continuation can pick the work up without re-reading raw history.
# 日本語訳: turn-budget の force-close 呼び出しが常に送る唯一のシステム
#      プロンプト。「何が完了したか／成果物の場所／残り作業／繰り返すべき
#      でないこと」を圧縮して伝え、新しい継続がそのまま引き継げるようにする。
WRAP_UP_SYSTEM_PROMPT = """\
You are being asked to WRAP UP the current unit of work. Do NOT continue the \
task and do NOT request or call any further tools or operations. Your only job \
now is to consolidate what has happened so far into a single, final hand-off \
so a fresh continuation can pick the work up without re-reading the raw history.

Cover, compactly:

- What is DONE — the essential conclusions, findings, and results produced so \
far, distilled as knowledge. Summarise large inputs you read down to what \
matters; do not paste their contents back.
- Where the OUTPUTS live — reference any files or stored artifacts by their \
location rather than inlining large data.
- What REMAINS — the next concrete step(s) still needed to finish.
- What must NOT be repeated — actions already completed that a continuation \
should not redo.

Keep it concise and self-contained, and prefer references over large inline \
content. This consolidation replaces the raw working history for the next step, \
so anything omitted here is lost: capture the essence, not the volume."""


def wrap_up_system_prompt(reason: "str | None" = None) -> str:
    """The axis-independent wrap-up system prompt (the single SP of §8).

    Exposed as a function (not just the constant) so callers depend on a stable
    surface and a future templated variant stays source-compatible.

    Args:
        reason: Optional cause for the wrap-up (e.g. "router reached iteration
            limit (5)"). When provided, prepended to the SP so the LLM knows
            WHY it is being asked to wrap up. Placed in the SP (not as a
            trailing user message) to avoid breaking provider function-call
            pairing rules (Gemini rejects a user turn immediately after a
            tool_result). ``None`` (= cumulative-axis path) keeps the prompt
            cause-neutral so existing replay fixtures are unaffected.
    """
    if reason is None:
        return WRAP_UP_SYSTEM_PROMPT
    return f"This wrap-up is triggered because: {reason}.\n\n{WRAP_UP_SYSTEM_PROMPT}"
