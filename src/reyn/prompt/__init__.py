"""``reyn.prompt`` — the human-reviewable home for reyn's LLM-facing system-prompt
(SP) content.

**Phase 1** relocated the **agent-facing** SP sources — the ones a live
end-user agent actually runs on every turn (§A-D: router_frame, universal_slots,
codeact, retrieval). **Phase 2 (this state of the package)** adds the
**internal-service** SPs — compaction, turn-budget wrap-up, and judge_output's
scorer template (§E-G). The loop-control nudges (§I-M) and dev/dogfood SP (§H)
remain out of scope (Phase 3 — they inject mid-request-stream, a trickier shape).

Byte-identical relocation: every string below is an EXACT copy of what its
source builder previously inlined — no LLM-facing wording changed. The
assembling *logic* (gating conditionals, ``"\\n".join``, dynamic f-string
interpolation of runtime values like ``agent_name``/``rubric``) stays in the
original builder module; this package holds only the literal text (plus, for
R1–R4-shaped families, the small parameterized function that selects among a
gated content family — see each module's docstring for the exact split).

Reviewer's map — every SP source in the codebase, one row per module:

| module               | feeds (builder)                                                          | when/where/why (one line)                                                                 |
|----------------------|---------------------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| ``router_frame.py``  | ``reyn.runtime.router_system_prompt.build_system_prompt``                 | the OS-frame: identity, static Behaviour core, ambiguity rule (2 variants), memory-guidance bullet, project_context labels, cwd-instruction sentence, output-language directive. Always-on OS frame, called once per turn. |
| ``universal_slots.py``| ``reyn.tools.schemes._universal_sp.build_universal_tool_use_slots``       | the 6 scheme-owned tool-use SP slots (R1 Capabilities routing guide, R2 Action categories, R3 never-invent + ROUTING RULE, R4 cwd HOW clause, plus the Skills block) — filled by universal-category / enumerate-all / retrieval. |
| ``codeact.py``        | ``reyn.tools.schemes.codeact._render_code_api``                           | CodeAct's static code-API instructional header (the per-entry function-signature loop stays in the scheme module — it renders LIVE catalog data, not fixed text). |
| ``retrieval.py``      | ``reyn.tools.schemes.retrieval._search_sp``                               | retrieval's search-guidance SP — 2 variants (non-terminal "search first" / terminal "call a presented match"), gated on RePresent convergence. |
| ``compaction.py``     | ``reyn.services.compaction.engine`` (main + resummarize + phase-results)  | the 3 compaction-family summariser SPs — rolling-summary compaction, the overshoot re-summarize pass (#271 T2), and the phase act-loop control_ir_results summariser (PR-N5). |
| ``turn_budget.py``    | ``reyn.services.turn_budget.engine.wrap_up_system_prompt``                | the axis-independent force-close wrap-up SP (#1092 §8); the ``reason`` variant is the same text with a cause line prepended, not a separate string. |
| ``judge.py``          | ``reyn.core.op_runtime.judge_output``                                    | the judge_output scorer's static evaluator-instructions + "Rubric:" label header; the caller-supplied rubric body itself stays dynamic content interpolated at call time, not relocated. |

**Not yet relocated (Phase 3, out of scope for this state)**: the loop-control
nudge SPs (§I-M — they inject mid-request-stream) and the dev/dogfood SP (§H).

Verification: byte-identical relocation is proven per-phase by an add+remove
scaffold golden-diff test (Phase 1:
``tests/scaffold/test_sp_prompt_package_phase1_byte_identical.py``, already
removed once green; Phase 2:
``tests/scaffold/test_sp_prompt_package_phase2_byte_identical.py``) plus the
permanent F1 coverage gate (``tests/test_sp_prompt_package_coverage.py``) and
CJK-free + SP-tool-name-liveness gates (``tests/test_llm_facing_text_english_only.py``,
#2860), which continue to scan the assembled agent-facing output post-relocation.
"""
from __future__ import annotations

from reyn.prompt._types import PromptComponent
from reyn.prompt.codeact import CODEACT_STATIC_HEADER
from reyn.prompt.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    PHASE_COMPACTION_SYSTEM_PROMPT,
    RESUMMARIZE_SYSTEM_PROMPT,
)
from reyn.prompt.judge import JUDGE_EVALUATOR_HEADER, RUBRIC_LABEL_PREFIX, judge_system_prompt
from reyn.prompt.retrieval import SEARCH_SP_NON_TERMINAL, SEARCH_SP_TERMINAL
from reyn.prompt.router_frame import (
    AMBIGUITY_RULE_INTERACTIVE,
    AMBIGUITY_RULE_NON_INTERACTIVE,
    BEHAVIOUR_STATIC_CORE,
    CWD_REFERENCE_MAPPING_PREFIX,
    DEFAULT_CWD_HOW_CLAUSE,
    ERRORS_VERBATIM_RULE,
    IDENTITY_PREAMBLE,
    MEMORY_GUIDANCE_BULLET,
    NO_FABRICATION_RULE,
    PROJECT_CONTEXT_HEADER,
    PROJECT_CONTEXT_PREFERENCE_NOTE,
    TASK_COMPLETION_RULE,
    ambiguity_rule,
    cwd_reference_mapping_sentence,
    output_language_directive,
    role_stamp,
)
from reyn.prompt.turn_budget import WRAP_UP_SYSTEM_PROMPT, wrap_up_system_prompt
from reyn.prompt.universal_slots import (
    build_action_categories_slot,
    build_behaviour_slot,
    build_capabilities_routing_guide,
    build_environment_how_clause,
    build_skills_slot,
)

__all__ = [
    "PromptComponent",
    "CODEACT_STATIC_HEADER",
    "COMPACTION_SYSTEM_PROMPT",
    "PHASE_COMPACTION_SYSTEM_PROMPT",
    "RESUMMARIZE_SYSTEM_PROMPT",
    "JUDGE_EVALUATOR_HEADER",
    "RUBRIC_LABEL_PREFIX",
    "judge_system_prompt",
    "WRAP_UP_SYSTEM_PROMPT",
    "wrap_up_system_prompt",
    "SEARCH_SP_NON_TERMINAL",
    "SEARCH_SP_TERMINAL",
    "AMBIGUITY_RULE_INTERACTIVE",
    "AMBIGUITY_RULE_NON_INTERACTIVE",
    "BEHAVIOUR_STATIC_CORE",
    "CWD_REFERENCE_MAPPING_PREFIX",
    "DEFAULT_CWD_HOW_CLAUSE",
    "ERRORS_VERBATIM_RULE",
    "IDENTITY_PREAMBLE",
    "MEMORY_GUIDANCE_BULLET",
    "NO_FABRICATION_RULE",
    "PROJECT_CONTEXT_HEADER",
    "PROJECT_CONTEXT_PREFERENCE_NOTE",
    "TASK_COMPLETION_RULE",
    "ambiguity_rule",
    "cwd_reference_mapping_sentence",
    "output_language_directive",
    "role_stamp",
    "build_action_categories_slot",
    "build_behaviour_slot",
    "build_capabilities_routing_guide",
    "build_environment_how_clause",
    "build_skills_slot",
]
