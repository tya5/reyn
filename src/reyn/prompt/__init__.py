"""``reyn.prompt`` — the human-reviewable home for reyn's LLM-facing system-prompt
(SP) content.

**Phase 1 (this state of the package)** relocates the **agent-facing** SP sources —
the ones a live end-user agent actually runs on every turn. Internal-service SPs
(compaction/turn-budget/judge_output) are a later phase, not yet relocated (see
the "Not yet relocated" note below).

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

**Not yet relocated (Phase 2+, out of scope for this state)**: compaction-family
SPs (``services/compaction/engine.py``), the turn-budget wrap-up SP
(``services/turn_budget/engine.py``), and the ``judge_output`` rubric-evaluator
template (``core/op_runtime/judge_output.py``) — all internal-service LLM calls,
not agent-turn SP.

Verification: byte-identical relocation is proven by the golden-diff scaffold
test (``tests/scaffold/test_sp_prompt_package_phase1_byte_identical.py`` —
triggered_by/removed_by this PR) plus the pre-existing permanent CJK-free +
SP-tool-name-liveness gates (``tests/test_llm_facing_text_english_only.py``,
#2860), which continue to scan the assembled output post-relocation and were
extended with a bare-backtick-token watch-list meta-guard.
"""
from __future__ import annotations

from reyn.prompt._types import PromptComponent
from reyn.prompt.codeact import CODEACT_STATIC_HEADER
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
