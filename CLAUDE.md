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
- **`docs/reference/runtime/control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`** in `src/reyn/op_runtime/registry.py`. New op kinds get a section in the reference in the same PR.

## Testing policy (READ BEFORE WRITING TESTS)

The testing policy is at **`docs/deep-dives/contributing/testing.ja.md`** (English:
`docs/deep-dives/contributing/testing.md`). It is normative — read it before adding
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

## PR workflow (READ BEFORE OPENING / REVIEWING A PR)

This repo is touched by multiple Claude sessions (lead-coder, e2e-coder,
per-PR coders) authenticating as the same `gh` user. Three rules keep that
coherent:

1. **Finish your own Test plan before merge.** PR authors run every
   Manual / Visual item in the Test plan and tick the box, or replace
   `- [ ]` with `- [x] (skipped — <reason>)`. Reviewers do not merge
   while items are unchecked without an explicit waiver.
2. **Role-prefix every issue / PR body / PR comment.** Start the PR
   body AND each follow-up comment with `**[role-name]** — ` (e.g.
   `[lead-coder]`, `[e2e-coder]`, `[tui-coder]`, `[dogfood-coder]`,
   `[per-PR-coder]`, `[security-reviewer]`) so the recipient session
   can tell "this is feedback for me" from "I wrote this earlier
   myself". **The PR body counts** — it is the first comment a
   reviewing session reads, and without the prefix the role of the
   author can only be inferred from branch naming (= a hint, not the
   workflow contract). The `Co-Authored-By: Claude` commit trailer
   does not propagate to PR comments, so this prefix is the only
   cross-session signal.
3. **If broker MCP is connected, supplement PR comments with
   `post_message` for time-sensitive coordination.** When a session
   would otherwise wait for the peer's next manual polling to notice
   a block or revision-ready signal, send a parallel broker message
   (= `post_message(to=<peer>, ...)` with a short summary). Typical
   uses: "revision pushed, ready for re-review", "block raised on
   #N", "I'm picking up #M, please pause on it". **PR comments
   remain the authoritative audit trail** — review decisions (block /
   accept / merge), revision rationale, and review evidence all stay
   in PR body / comments / commit messages. Broker is only for
   reducing reviewer-side latency. When broker is unavailable or the
   peer is offline, fall back to PR comment alone — the workflow
   still works.

   **Broker semantic limits (= treat broker as best-effort hint):**
   - **In-memory only**: broker process restart drops all queued
     messages. If the broker maintainer announces a restart, every
     session must rewrite any in-flight coordination signal as a PR
     comment so the contract is preserved.
   - **Up to ~30s lag**: watcher polls at ~30s intervals, so a
     "block raised on #N" race with the peer's `git push` is not
     fully closed by broker alone. Truly critical pause / block /
     "do not merge" signals must land on the PR comment **and** via
     broker — never broker alone.
   - **No ack semantics**: `post_message` returns `queued` only.
     The sender cannot tell whether the recipient has read or acted
     on the message. For coordination that must be confirmed, ask
     the peer to ack via broker reply, or verify the outcome on the
     PR (= comment posted, push paused, etc.). Do not assume "I
     posted, therefore they paused".

   In one line: **broker = hint, PR = contract**.

## Pre-conclusion observation checklist (READ BEFORE WRITING ANY FINDING / 結論)

**Active trigger**: when you are about to write any of the following — **STOP**
and run the checklist below before continuing:

- 結論 / conclusion / finding / 確定 / decisive
- パターン / pattern / 一貫して / consistently
- 100% / 全件 / N/N / 0% / all / every
- proven / validated / confirmed / 決定的
- attractor / hallucination / regression (= behavioral classification)

**5-question checklist**:

1. List each specific observation that supports the claim — can you?
2. Is each observation **primary data** (= events log / metric / direct
   output) or **inference** from other observations? Inference chains
   downgrade "verified" to "hypothesised".
3. Did you actively look for data that **falsifies** the claim?
4. Is the observation infrastructure (= trace dump / events log /
   metric) actually capturing what you'd need to support the claim?
5. If you're about to write "N/N" or "100%", did you directly inspect
   each of the N items, or did you inspect 1-2 and extrapolate?

**Re-frame instead of overstating**:

- ❌ "X happens 100% in condition Y" (= when only 1-2 of N inspected)
- ✅ "Hypothesis: X may dominate in Y. Direct verification: 1/N. Remaining
  N-1 inspection pending."

**Reference**:
- `feedback_pre_conclusion_observation_checklist.md` (full 5-question detail
  + failure-mode patterns)
- `feedback_observe_before_speculate_llm.md` (passive principle this trigger
  operationalises)

## Skill resolution order

1. `reyn/project/<name>/skill.md` — checked-in project skills
2. `reyn/local/<name>/skill.md` — workspace-local (typically gitignored)
3. `src/stdlib/skills/<name>/skill.md` — bundled stdlib skills

`@sub_skill` graph nodes and `run_skill` Control IR ops use the same lookup.

## When in doubt — read these (Tier 2)

- **P1–P8 rationale and examples**: `docs/concepts/principles.md`
- **Care boundary** (what Reyn cares / doesn't care): `docs/concepts/care-boundary.md`
- **Architecture overview / component layers**: `docs/concepts/architecture.md`
- **Phase vs Skill vs OS boundary**: `docs/concepts/phase-vs-skill-vs-os.md`
- **Why constrain the LLM (P4)**: `docs/concepts/llm-as-decision-engine.md`
- **Workspace** (P5): `docs/concepts/workspace.md`
- **Events / replay** (P6): `docs/concepts/events.md`
- **Permission model**: `docs/concepts/permission-model.md`
- **Input handling, ask_user, Phase Preprocessor (run_op / iterate / validate
  / lint_plan / python)**: read the corresponding stdlib skill (`skill_router`,
  `eval`, `skill_improver`) for live examples
- **ContextFrame / Output schemas**: `src/reyn/models.py`
- **Op catalog and dispatch**: `src/reyn/op_runtime/`

## Goal

> Phase transitions driven by LLM + constrained context are stable and valid.

The OS is the constant. Skills come and go. New skills MUST NOT require OS
code changes (P7).
