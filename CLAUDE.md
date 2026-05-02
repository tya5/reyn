# CLAUDE.md — Reyn Agent OS rules

Tier 1 hard rules for code-writing agents. Read on demand for rationale and
deep dives via the references at the bottom.

## Architecture

`User → Agent → Skill → OS → Phase → Workspace`, with Events recording every
state change. The OS is the constant; Skills come and go. New skills MUST NOT
require OS changes (P7).

## P1–P8 (CRITICAL — violations break the OS)

- **P1** Phase declares only `input_schema` and instructions. It MUST NOT know
  next phase, output schema, or its parent skill.
- **P2** Skill declares `entry_phase`, `graph`, `final_output_schema`. Phase
  connections live in Skill, NEVER in Phase.
- **P3** OS is the runtime engine — context build, LLM call, validation,
  Control IR execution, transitions, events. Skills and LLM do not run things.
- **P4** LLM picks ONLY from OS-provided candidates: next phase + artifact +
  control_ir. No arbitrary next phases.
- **P5** Phase has NO output schema. Output shape is determined by
  `next_phase.input_schema` or `skill.final_output_schema`.
- **P6** Only Skill defines `final_output_schema`. OS validates final output
  against it.
- **P7 (CRITICAL)** OS code MUST NOT contain skill-specific strings (phase
  names, artifact types, fields). **Detection rule**: if a literal naming a
  specific phase / artifact type / field appears in OS code, it's a violation.
  Common traps:
  - Fallback logic that fabricates skill-specific fields → return raw artifact
  - Decision vocabulary encoding skill concepts (`decision="revise"`) → use
    OS-level only: `continue | finish | abort`
  - Hardcoded artifact type names in any OS module
- **P8** Phase instructions describe WHAT/WHEN/domain rules. They MUST NOT
  enumerate output artifact fields or describe Control IR format. The OS
  injects those at runtime via `candidate_outputs` and `available_control_ops`.

## LLM Output Contract (REJECTED if violated)

Single format for all phases:

```json
{
  "control": {
    "type": "transition|finish|abort",
    "decision": "continue|finish|abort",
    "next_phase": "<name> or null",
    "confidence": 0.0,
    "reason": {"summary": "..."}
  },
  "artifact": {"type": "<schema_name>", "data": {}},
  "control_ir": []
}
```

- `decision` values are OS-level only: `continue | finish | abort`. **`revise`
  is NOT valid** — it encodes a skill-specific concept (P7). Transitions to a
  "revise" phase use `decision="continue"`.
- Consistency rules:
  - `type=finish` → `decision=finish`, `next_phase=null`
  - `type=transition` → `next_phase` non-null
  - `type=abort` → `decision=abort`, `next_phase=null`

## Validation (MANDATORY)

- **Transition**: `next_phase` allowed by Skill graph; artifact matches
  `next_phase.input_schema`.
- **Finish**: finishing allowed; final_output matches
  `skill.final_output_schema`.

## Hard "NEVER" rules (cross-refs to P-numbers)

- NEVER define transitions inside Phase (P1)
- NEVER define output schema inside Phase (P5)
- NEVER allow LLM to choose arbitrary next phase (P4)
- NEVER put skill-specific strings in OS code (P7)
- NEVER enumerate artifact fields in Phase instructions (P8)
- NEVER describe Control IR format in Phase instructions (P8)
- ALWAYS validate LLM output (Transition + Finish above)
- ALWAYS emit events for state changes (P3)

## Skill resolution order

1. `reyn/project/<name>/skill.md` — checked-in project skills
2. `reyn/local/<name>/skill.md` — workspace-local (typically gitignored)
3. `src/stdlib/skills/<name>/skill.md` — bundled stdlib skills

`@sub_skill` graph nodes and `run_skill` Control IR ops use the same lookup.

## When in doubt — read these (Tier 2)

- **P1–P8 rationale and examples**: `docs/en/concepts/principles.md`
- **Architecture overview / component layers**: `docs/en/concepts/architecture.md`
- **Phase vs Skill vs OS boundary**: `docs/en/concepts/phase-vs-skill-vs-os.md`
- **Why constrain the LLM (P4)**: `docs/en/concepts/llm-as-decision-engine.md`
- **Event model / replay**: `docs/en/concepts/events.md`
- **Workspace**: `docs/en/concepts/workspace.md`
- **Permission model**: `docs/en/concepts/permission-model.md`
- **Input handling, ask_user, Phase Preprocessor (run_op / iterate / validate
  / lint_plan / python)**: read the corresponding stdlib skill (`skill_router`,
  `eval`, `skill_improver`) for live examples
- **ContextFrame / Output schemas**: `src/reyn/models.py`
- **Op catalog and dispatch**: `src/reyn/op_runtime/`

## Goal

> Phase transitions driven by LLM + constrained context are stable and valid.

The OS is the constant. Skills come and go. New skills MUST NOT require OS
code changes (P7).
