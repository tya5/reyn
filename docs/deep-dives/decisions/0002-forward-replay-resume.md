# ADR-0002: Forward-replay resume (no phase-head re-execution)

**Status**: Accepted (2026-05-02)
**Track**: D-track (D3b)

## Context

When a skill resumes after a crash, two extreme strategies exist:

- **Re-run the whole skill.** Simple, but wastes all completed work and
  re-pays LLM cost from scratch.
- **Restart from exactly where the crash happened.** Ideal but requires
  full per-step state capture (= huge WAL volume).

The middle ground — "fast-forward to the in-flight phase, re-run that
phase from start with op-level memoization" — is what real systems use
(database WAL recovery, journaling FS, distributed log replay).

We needed to commit to a specific point on this spectrum.

## Considered alternatives

- **A. Re-run the whole skill from entry_phase.** Rejected: discards
  hours of progress for long-running skills. Reyn's headline value is
  multi-phase orchestration; resume must preserve completed phases.
- **B. Resume from exact step (= step-level fast-forward).** Rejected:
  requires every step's intermediate state to be in the WAL so resume
  can pick up mid-act-turn. WAL volume balloons; complexity high.
- **C. Phase-level fast-forward + op memoization within the in-flight
  phase.** Adopted. Skip completed phases entirely. Re-enter the
  in-flight phase from its start, but use the WAL's `step_completed`
  records to memoize ops that already succeeded (dispatch_tool returns
  recorded result without re-invoking).

## Decision

**Adopt C: phase-level fast-forward + op memoization.**

Resume mechanics:

1. `SkillRegistry.load_active()` discovers in-flight runs.
2. `SkillResumeAnalyzer.analyze` builds a `ResumePlan` from the snapshot
   (where `current_phase` was at crash) and WAL events for that run.
3. `SkillResumeCoordinator.decide_for_plan` applies operator policy
   (skip / retry / discard / resume / prompt_required).
4. `OSRuntime.run(resume_plan=...)`:
   - Skips entry_phase loop
   - Sets `current_phase = resume_plan.current_phase`
   - Restores `_visit_counts` and `_history` from the plan
   - Loads `last_phase_artifact_path` as the input artifact
   - Re-enters that phase from its start
5. Within the re-entered phase, `dispatch_tool` and
   `_call_llm_and_record` use `committed_steps` as memo — already-done
   ops/LLM calls return their recorded result without re-execution.

Non-goals: re-running a partial act turn. The in-flight phase loops act
turns from its start; the memoization makes that cost-free for the
already-committed portion.

## Consequences

**Positive:**

- Long skills resume in seconds instead of re-running hours of work.
- LLM cost on resume = cost of work that genuinely hadn't been done
  yet (memo hits suppress duplicate billing).
- WAL volume grows with completed steps, not with every internal state
  transition.
- Phase boundaries become natural truncation points — the per-phase
  WAL truncation policy lives on top of this.

**Negative:**

- Within the in-flight phase, the runtime re-walks act turns from start.
  If a phase has 100 act turns and crashed at turn 99, the loop
  iterates 99 times (cheap — each iteration just checks memo). For
  most skills this is acceptable; pathological cases tracked as future
  optimization.
- The "ambiguous step" problem (step_started without step_completed)
  is real and unique to side-effect ops (see ADR-0003 op purity).
  Required separate mechanism (ADR-0007 prompt UX, R-D3 / R-D5).

**Precluded:**

- Mid-act-turn resume. If the LLM was invoked but the response wasn't
  recorded, the LLM is called again. Acceptable trade-off — LLM
  call-time persistence would require streaming-state capture which
  is out of scope.

## References

- Commit `4a1c0a8` — D3b-3 OSRuntime fast-forward
- Commit `47847db` — D3b-4 e2e crash → resume → completion test
- [docs/en/concepts/skill-resume.md](../concepts/skill-resume.md)
- ADR-0001 (state model — the WAL that enables this)
- ADR-0004 (memoization key)
