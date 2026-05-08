# ADR-0012: Auto-resume default + retry policy

**Status**: Accepted (2026-05-04). Supersedes [ADR-0007](0007-bulk-resume-prompt-ux.md).
**Track**: PR-resume-auto (commit `2177f55`)

## Context

[ADR-0007](0007-bulk-resume-prompt-ux.md) committed to a bulk 2-choice
prompt (`[continue all] / [abort all]`) on session start when one or
more skill_runs needed resume decisions. Before implementation, a
review pass questioned what the "abort all" path was for.

The justifications that had been carried forward from earlier
iterations:

1. **"Stale memo result locks skill into wrong answer"** — was solved
   structurally by [ADR-0011](0011-world-purity-memo-invalidation.md):
   `world` ops re-execute on resume, so the failure mode disappears.
2. **"Prior LLM responses bias the retry"** — examined and rejected as
   not actually rescuable by the prompt. LLM context anchoring is a
   property of LLM inference, not a crash-recovery problem; it exists
   identically when the user re-runs a skill from scratch in a fresh
   process. Reyn's state model has no act_turn-level rollback
   semantic, so there is no place for "wipe this turn's context" to
   live cleanly. The user's escape hatch is `/skill discard <id>`
   followed by re-invocation, which is what "abort all" would have
   offered anyway — but only as a side effect of the prompt, not as a
   primary affordance.
3. **"Nested skill chain disconnection"** (parent → run_skill(child),
   child needs reissue) — orthogonal to resume; tracked as future
   PR-discard-cascade-reissue.

With (1) structurally fixed and (2)/(3) not really "abort = restart"
problems, the prompt was offering a binary the user didn't need to
answer. Adding it just to acknowledge "we noticed something happened"
imposes cognitive load with no decision payoff.

## Considered alternatives

- **A. Keep ADR-0007's 2-choice prompt.** Forces explicit
  acknowledgement, but with no actionable decision the user routinely
  picks "continue" anyway. UX pessimism that doesn't earn its weight.
- **B. Auto-resume + slash escape hatches.** No prompt; restored
  skills resume in the background. Power users and abort cases use
  `/skill list` and `/skill discard <id>`. Operator-level policy lives
  in `reyn.yaml`.
- **C. Auto-resume + interactive prompt only when ambiguous step
  exists.** Compromise. But ambiguous step + retry policy is now the
  default for almost every skill author; the prompt would fire most
  resumes anyway.
- **D. Per-skill prompt with skip / retry / discard buttons.** Higher
  cognitive load than ADR-0007 already rejected.

## Decision

**Adopt B: auto-resume on session start; default policy = retry.**

Default policy (`reyn.yaml` `skill_resume.default`) shifts from
`prompt` (the original ADR-0007 design) to `retry`. With this default
in place, even ambiguous steps proceed without user input.

Implementation pieces:

- `ChatSession._auto_resume_active_skills` runs on session start,
  iterates `SkillResumeCoordinator.discover_and_decide`, and spawns
  one `asyncio.create_task` per resumable run via `_spawn_resumed_skill`.
- `SkillResumeConfig.default` literal changed from `"prompt"` to
  `"retry"` in `src/reyn/config.py`.
- The `discover_and_decide` step still produces `ResumeDecision`; the
  runtime's `apply_decisions` is what now handles `retry` / `skip` /
  `discard` actions cleanly without a prompt detour.
- Slash commands `/skill list` and `/skill discard <id>` are the user's
  escape hatch for selective abort.
- `--no-restore` and `--reset` CLI flags ([ADR-0010](0010-restore-cli-flags.md))
  remain the bypass options.

## Consequences

**Positive:**

- Zero prompts on session start. The common path (skill resumes,
  finishes, user sees result) requires no input.
- Skill author independence: no need to write good `description`
  fields just to make the prompt readable (an ADR-0007 negative).
- The `UserIntervention` model doesn't need extension to support bulk
  prompts (the latent design risk that drove ADR-0007's β scope cut).
- Operator-level control via yaml is sufficient for power users.

**Negative:**

- Users lose the "I was warned about an interruption" notification.
  Mitigated by `/skill list` showing active runs and the WAL events
  (`skill_run_interrupted`, `skill_run_resumed`) being audit-visible.
- `retry` default means ambiguous steps re-execute side-effect ops.
  For skills with risky side effects, the operator must override in
  `reyn.yaml`. Documented in the upgrade policy.
- A nested skill that the user wants to abort surgically requires
  finding the run_id (via `/skill list`) and using `/skill discard
  <id>` per run. This is heavier than a bulk prompt would have been
  for the rare "abort everything" case.

**Precluded:**

- Per-resume interactive UX. If a user need surfaces, the prompt path
  in `discover_and_decide` is still wired (`prompt_required` action
  exists in the type system) and could be re-enabled — but doing so
  reintroduces ADR-0007's UX trade-offs.

## References

- Commit `2177f55` — auto-resume implementation
- Commit `7e764ce` — PR-memo-purity-fix (the structural fix that made
  this design viable; see ADR-0011)
- [ADR-0007](0007-bulk-resume-prompt-ux.md) — superseded
- [ADR-0011](0011-world-purity-memo-invalidation.md) — the (1)
  resolution this ADR depends on
- discussion-log Phase 11
