"""``reyn.prompt`` — the human-reviewable home for reyn's LLM-facing system-prompt
(SP) content.

**Phase 1** relocated the **agent-facing** SP sources — the ones a live
end-user agent actually runs on every turn (§A-D: router_frame, universal_slots,
codeact, retrieval). **Phase 2** added the **internal-service** SPs —
compaction, turn-budget wrap-up, and judge_output's scorer template (§E-G).
**Phase 3 (this state of the package, FINAL)** adds the remaining two
categories: the loop-control / weak-model nudges that inject mid-REQUEST-
STREAM rather than into the assembled system prompt (§I-L:
``loop_control.py``, plus §M's CodeAct observation labels folded into
``codeact.py``), and the dev/dogfood internal eval harness's judge SPs (§H:
``dogfood.py``). **All 13 SP sources identified in the original inventory are
now relocated — the ``reyn.prompt`` package relocation arc is COMPLETE.**

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
| ``codeact.py``        | ``reyn.tools.schemes.codeact._render_code_api`` / ``_format_codeact_observation`` | §C: CodeAct's static code-API instructional header (the per-entry function-signature loop stays in the scheme module — it renders LIVE catalog data, not fixed text). §M (Phase 3): the fixed ``[codeact result/stdout/stderr]`` observation-turn labels (mid-request-stream). |
| ``retrieval.py``      | ``reyn.tools.schemes.retrieval._search_sp``                               | retrieval's search-guidance SP — 2 variants (non-terminal "search first" / terminal "call a presented match"), gated on RePresent convergence. |
| ``compaction.py``     | ``reyn.services.compaction.engine`` (main + resummarize + phase-results)  | the 3 compaction-family summariser SPs — rolling-summary compaction, the overshoot re-summarize pass (#271 T2), and the phase act-loop control_ir_results summariser (PR-N5). |
| ``turn_budget.py``    | ``reyn.services.turn_budget.engine.wrap_up_system_prompt``                | the axis-independent force-close wrap-up SP (#1092 §8); the ``reason`` variant is the same text with a cause line prepended, not a separate string. |
| ``judge.py``          | ``reyn.core.op_runtime.judge_output``                                    | the judge_output scorer's static evaluator-instructions + "Rubric:" label header; the caller-supplied rubric body itself stays dynamic content interpolated at call time, not relocated. |
| ``loop_control.py``   | ``reyn.runtime.router_loop.RouterLoop`` / ``reyn.llm.llm._apply_g12_signal`` / ``reyn.runtime.reasoning_continuity`` | §I-L (Phase 3): the empty-stop retry directive ("resume"), the G12 post-tool continuation/error signals (success cell = same "resume" token by design), the tool-call-cap re-grounding notice, and the reasoning-continuity section header + framing sentence. ALL inject mid-request-stream (synthetic messages / embedded tool-result text), not via the system-prompt assembler — verified byte-identical at each own injection point, not the system-prompt golden diff. |
| ``dogfood.py``        | ``reyn.dev.dogfood.interpretation.generate_interpretation`` / ``reyn.dev.dogfood.verifiers.reply._default_judge_fn`` | §H (Phase 3): the internal dogfood eval harness's two LLM-judge SPs (per-scenario 3-line interpretation; reply-verifier rubric scorer). Dev-tool-only, not surfaced to an end-user agent, but IS LLM-facing (reaches a real LLM request) — in scope per owner's "全て (all of them)" instruction. |

Verification: byte-identical relocation is proven per-phase by an add+remove
scaffold golden-diff test (Phase 1:
``tests/scaffold/test_sp_prompt_package_phase1_byte_identical.py``, already
removed once green; Phase 2:
``tests/scaffold/test_sp_prompt_package_phase2_byte_identical.py``, likewise
removed) plus the permanent F1 coverage gate
(``tests/test_sp_prompt_package_coverage.py``) and CJK-free +
SP-tool-name-liveness gates (``tests/test_llm_facing_text_english_only.py``,
#2860), which continue to scan the assembled agent-facing output
post-relocation. Phase 3's mid-request-stream nudges (§I-M) are NOT covered by
the system-prompt golden diff or corpus — each is verified byte-identical at
ITS OWN injection point by a dedicated per-nudge test
(``tests/test_sp_prompt_package_loop_control_nudges.py``), and the CJK/
liveness gate's corpus is extended to include them (see that test module).
"""
from __future__ import annotations

from reyn.prompt._types import PromptComponent
from reyn.prompt.codeact import (
    CODEACT_RESULT_LABEL,
    CODEACT_STATIC_HEADER,
    CODEACT_STDERR_LABEL,
    CODEACT_STDOUT_LABEL,
)
from reyn.prompt.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    PHASE_COMPACTION_SYSTEM_PROMPT,
    RESUMMARIZE_SYSTEM_PROMPT,
)
from reyn.prompt.dogfood import (
    DOGFOOD_INTERPRETATION_SYSTEM_PROMPT,
    DOGFOOD_JUDGE_EVALUATOR_HEADER,
    DOGFOOD_JUDGE_RUBRIC_LABEL_PREFIX,
    dogfood_judge_system_prompt,
)
from reyn.prompt.judge import JUDGE_EVALUATOR_HEADER, RUBRIC_LABEL_PREFIX, judge_system_prompt
from reyn.prompt.loop_control import (
    EMPTY_STOP_RETRY_DIRECTIVE,
    G12_SIGNAL_ERROR_TEXT,
    G12_SIGNAL_TEXT,
    REASONING_CONTINUITY_HEADER,
    REASONING_CONTINUITY_NOTE,
    tool_call_cap_notice,
)
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
    "CODEACT_RESULT_LABEL",
    "CODEACT_STDOUT_LABEL",
    "CODEACT_STDERR_LABEL",
    "COMPACTION_SYSTEM_PROMPT",
    "PHASE_COMPACTION_SYSTEM_PROMPT",
    "RESUMMARIZE_SYSTEM_PROMPT",
    "DOGFOOD_INTERPRETATION_SYSTEM_PROMPT",
    "DOGFOOD_JUDGE_EVALUATOR_HEADER",
    "DOGFOOD_JUDGE_RUBRIC_LABEL_PREFIX",
    "dogfood_judge_system_prompt",
    "JUDGE_EVALUATOR_HEADER",
    "RUBRIC_LABEL_PREFIX",
    "judge_system_prompt",
    "EMPTY_STOP_RETRY_DIRECTIVE",
    "G12_SIGNAL_TEXT",
    "G12_SIGNAL_ERROR_TEXT",
    "REASONING_CONTINUITY_HEADER",
    "REASONING_CONTINUITY_NOTE",
    "tool_call_cap_notice",
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
