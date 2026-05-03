# CLAUDE.md — Reyn Agent OS rules

Tier 1 hard rules for code-writing agents. Read on demand for rationale and
deep dives via the references at the bottom.

## Architecture

`User → Agent → Skill → OS → Phase → Workspace`, with Events recording every
state change. The OS is the constant; Skills come and go. New skills MUST NOT
require OS changes (P7).

## P1–P8 (CRITICAL — violations break the OS)

- **P1** Phase declares only `input_schema` and instructions. It MUST NOT
  know its next phase, output schema, or parent skill. Output shape is
  determined externally — by `next_phase.input_schema` or by the skill's
  `final_output_schema`.
- **P2** Skill declares `entry_phase`, `graph` (allowed transitions), and
  `final_output_schema`. Phase connections live in Skill, never in Phase.
  Final-output validation is the OS's responsibility against this schema.
- **P3** OS is the runtime engine — context build, LLM call, validation,
  Control IR execution, transitions, events. Skills and the LLM do not run
  things; they describe and decide.
- **P4** LLM picks ONLY from OS-provided candidates: next phase + artifact +
  control_ir. No arbitrary next phases.
- **P5 (Workspace is the single source of truth)** All data, artifacts, and
  files passed between phases live in the workspace. Phases read and write
  only through Control IR (gated by the permission system). In-memory state
  inside a phase is not trustworthy until it lands in the workspace — this is
  what makes permission enforcement and crash recovery (PR21) possible.
- **P6 (Events are the audit truth)** Every state change emits an event. The
  event log (`events/`) is append-only and replay-capable. State recovery
  (crash recovery, audit trails, future hash chain), debugging, and
  cross-agent tracing all derive from events. Anything that mutates state
  without an event is invisible to the OS.
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

- NEVER define transitions or output schema inside Phase (P1)
- NEVER allow LLM to choose arbitrary next phase (P4)
- NEVER pass data between phases outside the workspace (P5)
- NEVER mutate runtime state without emitting an event (P6)
- NEVER put skill-specific strings in OS code (P7)
- NEVER enumerate artifact fields in Phase instructions (P8)
- NEVER describe Control IR format in Phase instructions (P8)
- ALWAYS validate LLM output (Transition + Finish above)
- ALWAYS emit events for state changes (P6)

## Testing policy (READ BEFORE WRITING TESTS)

The testing policy is at **`docs/ja/contributing/testing.md`** (English:
`docs/en/contributing/testing.md`). It is normative — read it before adding
or modifying tests.

Key constraints (full rationale in the doc):

- Each test belongs to exactly one Tier (1: Contract / 2: OS invariant /
  3: LLM-replay behavior). Anything that doesn't fit a Tier is **Tier 4 —
  do not write**.
- NEVER use `unittest.mock.MagicMock` / `AsyncMock` / `patch` to fake
  collaborators. Use real instances or the `LLMReplay` Fake. Mocks bypass
  real API contracts and silently rot.
- NEVER assert on private state (`tracker._daily_tokens == 100`,
  `mgr._timers["c1"]`, `reg._active[id]`). Use the public surface or a
  `snapshot()`-style read.
- NEVER pin algorithm-level behavior (sort order, dict iteration order,
  internal cache structure, exact whitespace / formatting).
- NEVER add snapshot / golden-file tests outside `tests/scaffold/`.
- Tests for an extracted refactor belong in `tests/scaffold/` with
  `triggered_by` / `removed_by` metadata, and are **deleted in the PR
  that lands the refactor**.
- Each test docstring's first line must declare its Tier:
  `"""Tier 3a: ..."""`.

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
- **Workspace** (P5): `docs/en/concepts/workspace.md`
- **Events / replay** (P6): `docs/en/concepts/events.md`
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
