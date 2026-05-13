# FP-0020: OSRuntime Layer Decomposition — Splitting runtime.py into Vertical Layers

**Status**: **done** — All 4 components LANDED 2026-05-13/14: RunState (`1dac280`) + LLMCallRecorder (`5628993`) + PhaseExecutor (`7e51216`) + RunOrchestrator (`929d81f`). runtime.py 1882 → 507 LoC (-73%); 6 new kernel modules: run_state / rollback_state / runtime_types / llm_call_recorder / phase_executor / run_orchestrator
**Proposed**: 2026-05-11
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`src/reyn/kernel/runtime.py` is 1,882 lines containing a single class (`OSRuntime`) that
simultaneously owns LLM call logic, WAL recording, budget enforcement, phase lifecycle,
act/decide loops, rollback, skill-node dispatch, and resume fast-forward. Unlike
`session.py` (FP-0016), where mixed responsibilities sit side by side, `runtime.py`'s
complexity is vertical: each layer calls the one below. This proposal decomposes that
depth into four focused files — `RunState`, `LLMCallRecorder`, `PhaseExecutor`,
`RunOrchestrator` — reducing `runtime.py` itself to ~400 lines. Total line count across
the five files increases slightly (~1,620 vs 1,882), but any file can be read in isolation,
which is the primary goal for AI-assisted coding workflows.

---

## Motivation

### The AI coding case for decomposition

An AI coding agent reading `runtime.py` to fix a WAL memo-lookup bug must load ~1,900 lines
of context covering budget hooks, rollback state, act-turn limits, postprocessor resume, and
MCP teardown — none of which are relevant to the bug. With the four-file decomposition, the
same agent reads `llm_call_recorder.py` (~350 lines) and is done.

This is the primary driver: total line count is explicitly allowed to increase if it reduces
the lines any single agent must hold in context.

### Vertical complexity vs horizontal mixing

`session.py` (FP-0019) had five unrelated concerns at the same level.
`runtime.py` has one concern — executing a skill — expressed at five depth levels:

```
run()                           orchestrate phases in sequence
  └─ _execute_phase()           drive one phase to completion
       └─ _run_act_loop()       execute act turns
            └─ _call_llm_and_record()  call LLM + record to WAL
                 └─ _check_budget_pre_llm()  enforce budget cap
```

Each level has a distinct unit of work and a natural seam where a new class begins.

### Existing good extraction: RollbackState

`RollbackState` (lines 126–221, ~95 lines) is already extracted as a separate class used
via `self._rollback`. This proposal follows the same pattern for the remaining clusters.

---

## Proposed implementation

### Component A — `RunState` (SMALL) — foundation

**LANDED** (commit `1dac280`): src/reyn/kernel/run_state.py + src/reyn/kernel/rollback_state.py

New file: `src/reyn/kernel/run_state.py`

`RunState` is a pure dataclass (no events, no I/O) holding all mutable state for one
`run()` invocation. Every layer receives the same `RunState` reference and mutates it
in place through well-named methods.

**Fields** (10 total):

```python
@dataclass
class RunState:
    # Navigation (owned by RunOrchestrator)
    visit_counts: dict[str, int]
    history: list[str]
    prev_phase: str | None
    rollback: RollbackState          # already extracted class

    # Per-phase lifecycle (reset by begin_phase())
    phase_started_at: float | None
    llm_call_idx_in_phase: int

    # Run-level accumulators
    token_usage: TokenUsage
    total_cost_usd: float

    # Safety extensions (FP-0005)
    # Key namespace:
    #   "max_phase_visits"         — loop limit (run-scoped)
    #   "phase_seconds"            — wall-clock budget (run-scoped)
    #   "max_act_turns:{phase}"    — act-turn limit (phase-scoped)
    safety_extensions: dict[str, float]

    # Trusted input (PR33 — set once at run() entry, never LLM-modified)
    skill_input: dict | None
```

**Methods** (~12): `begin_phase()`, `next_llm_invocation_id()`, `elapsed_phase_seconds()`,
`reset_phase_clock()`, `add_usage()`, `grant_extension()`, `effective_visit_cap()`,
`effective_phase_budget()`, `effective_act_turn_cap()`, `record_transition()`,
`restore_from_resume()`.

`restore_from_resume(plan, default_phase)` encapsulates the R-D2 pre-decrement pattern:
on resume, `visit_counts[current_phase]` is decremented by 1 before `begin_phase()` so
that the resumed phase's first LLM call lands on the same `op_invocation_id` as the
original run (memo-lookup correctness).

Target: `src/reyn/kernel/run_state.py` — ~100 lines

### Component B — `LLMCallRecorder` (SMALL) — Layer 3

**LANDED** (commit `5628993`): src/reyn/kernel/llm_call_recorder.py

New file: `src/reyn/kernel/llm_call_recorder.py`

Owns: one LLM call from budget pre-check through WAL recording.

```python
class LLMCallRecorder:
    def __init__(self, *, resolver, state_log, run_id, skill_registry,
                 budget_tracker, caller, chain_id, skill_name,
                 prompt_cache_enabled, events, skill): ...

    async def call(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict] | None,
        rollback_context: dict | None,
        state: RunState,
    ) -> dict:
        """Budget check → memo lookup → call_llm → WAL record → accumulate usage."""
```

Extracted methods: `_call_llm_and_record`, `_wal_step_completed_for_llm`,
`_extract_memoized_llm_result`, `_credit_budget_from_memo`, `_budget_agent_name`,
`_check_budget_pre_llm`, `_record_budget_post_llm`.

The phase-budget check (`_check_phase_budget`) moves **up** to `PhaseExecutor` (Layer 2)
so `LLMCallRecorder` has no dependency on `phase_started_at`. This is the only behavioral
change from the extraction — the check now happens before each LLM call at the PhaseExecutor
level rather than inside `_call_llm_and_record` itself. Observable behavior is identical.

Target: `src/reyn/kernel/llm_call_recorder.py` — ~350 lines

### Component C — `PhaseExecutor` (SMALL) — Layer 2

**LANDED** (commit `7e51216`): src/reyn/kernel/phase_executor.py + src/reyn/kernel/runtime_types.py (= leaf module for circular import avoidance)

New file: `src/reyn/kernel/phase_executor.py`

Owns: driving one phase to completion via act/decide loops with retry.

```python
class PhaseExecutor:
    def __init__(self, *, llm_caller: LLMCallRecorder, control_ir_executor,
                 events, skill, safety, intervention_bus): ...

    async def execute(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_phase_retries: int,
        artifact_path: str | None,
        rollback_context: dict | None,
        state: RunState,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Act loop → decide loop → return (result, output, retry_count)."""
```

Extracted methods: `_run_act_loop`, `_run_decide_with_retry`, `_execute_phase`,
`_check_phase_budget` (moved from `LLMCallRecorder`).

`_validate_phase_output` also moves here (currently an `OSRuntime` method but only called
from `_run_decide_with_retry`).

Target: `src/reyn/kernel/phase_executor.py` — ~270 lines

### Component D — `RunOrchestrator` (MEDIUM) — Layer 1

New file: `src/reyn/kernel/run_orchestrator.py`

Owns: phase sequence, transitions, rollback dispatch, skill-node dispatch, resume setup,
SkillRegistry lifecycle, exception handling.

```python
class RunOrchestrator:
    def __init__(self, *, phase_executor: PhaseExecutor, skill, workspace,
                 events, skill_registry, preprocessor, state: RunState,
                 safety, intervention_bus, resume_plan, run_id,
                 parent_run_id): ...

    async def run(
        self,
        initial_input: dict,
        output_language: str | None,
        max_phase_retries: int,
    ) -> RunResult: ...
```

Extracted methods: `_enter_phase`, `_handle_limit_checkpoint`, `_handle_rollback`,
`_finish_workflow`, `_fallback_final_output`, `_run_skill_node`, `_apply_skill_node`,
and the body of `run()` (currently 415 lines including resume fast-forward).

Target: `src/reyn/kernel/run_orchestrator.py` — ~500 lines

### Post-extraction OSRuntime

`OSRuntime` becomes a wiring layer:

```python
class OSRuntime:
    def __init__(self, skill, model, ...):
        state = RunState()           # fresh state
        llm_caller = LLMCallRecorder(...)
        phase_exec = PhaseExecutor(llm_caller=llm_caller, ...)
        self._orchestrator = RunOrchestrator(phase_executor=phase_exec, state=state, ...)
        # Public attributes retained for backward compat: workspace, events, control_ir_executor
        ...

    async def run(self, initial_input, ...) -> RunResult:
        return await self._orchestrator.run(initial_input, ...)

    # Retained: build_frame(), _build_candidates(), _effective_model()
    # These reference skill graph structure and are called from build_frame() consumers
```

Remaining responsibilities in `runtime.py`:
- Exception/type definitions (`LoopLimitExceededError`, `PhaseBudgetExceededError`,
  `WorkflowAbortedError`, `RunResult`, `_normalize_artifact`, `_validate_artifact_structure`)
- `OSRuntime.__init__` wiring (~120 lines)
- `OSRuntime.build_frame()` + `_build_candidates()` + `_effective_model()` (~115 lines)
- `OSRuntime.run()` delegation (~30 lines)

Target: `src/reyn/kernel/runtime.py` — ~400 lines

---

## Line count summary

```
Before (original proposal baseline)
  runtime.py             1,882 lines

After Components A/B/C (current — Component D still proposed)
  runtime.py             1,386 lines   (wiring + build_frame + types; ~1,490 projected after Component D)
  run_state.py             166 lines   (new — Component A, measured)
  rollback_state.py        111 lines   (new — Component A, derived entry not in original design)
  llm_call_recorder.py     415 lines   (new — Component B, measured)
  phase_executor.py        500 lines   (new — Component C, measured)
  runtime_types.py         105 lines   (new — Component C, leaf module for circular import avoidance)
  ──────────────────────────────────
  Subtotal (A/B/C landed) 2,683 lines across 6 files

After Component D (projected)
  runtime.py              ~400 lines   (projected)
  run_orchestrator.py     ~500 lines   (projected, new — Component D)
  ──────────────────────────────────
  Total (projected)      ~1,697 lines
```

Net after A/B/C: **runtime.py reduced by 496 lines** (1,882 → 1,386). Component D will
reduce it a further ~986 lines (to ~400) by extracting the orchestrator body.

**Goal**: minimize lines-per-file for AI coding agent context windows, not minimize total
lines. A +10% total increase that halves max-file size is a net win.

---

## Priority ordering

**A → B → C → D**

`RunState` (A) is the foundation — all other components receive it. `LLMCallRecorder` (B)
has no dependencies on C or D and is the highest-value extraction (WAL + budget in one
testable unit). `PhaseExecutor` (C) depends on B. `RunOrchestrator` (D) depends on B + C
and is the largest piece — done last.

Each wave can land as a standalone PR with no visible behavior change.

---

## Dependencies

- **None** for Component A. Standalone.
- **Component B**: requires A (`RunState`).
- **Component C**: requires A + B.
- **Component D**: requires A + B + C.
- **FP-0019** (`session.py` refactor): independent. Can proceed in parallel.
- **FP-0012** (async execution): LANDED (commit `c9e79d6`). Components B+C expose the
  internal LLM-call and phase boundary as stable units, which the async task infrastructure
  can now target cleanly.

---

## Cost estimate

| Component | Cost | Notes |
|---|---|---|
| A: `RunState` | SMALL | Pure data + methods, no behavior change |
| B: `LLMCallRecorder` | SMALL | Method extraction; `_check_phase_budget` moves up one layer |
| C: `PhaseExecutor` | SMALL | Method extraction + `_validate_phase_output` relocation |
| D: `RunOrchestrator` | MEDIUM | Largest extraction; resume + lifecycle complexity |
| Tests (Tier 1 per new class) | SMALL | LLMCallRecorder especially valuable to test in isolation |
| **Total** | **MEDIUM** | |

Component A alone is SMALL and can land first as the enabling foundation.

---

## Related

- `src/reyn/kernel/runtime.py` — extraction source (1,882 lines)
- `src/reyn/kernel/run_state.py` — new (Component A)
- `src/reyn/kernel/llm_call_recorder.py` — new (Component B)
- `src/reyn/kernel/phase_executor.py` — new (Component C)
- `src/reyn/kernel/run_orchestrator.py` — new (Component D)
- FP-0019 (`0019-chat-session-refactor.md`) — parallel God-file reduction for `session.py`
- ADR-0029 (Permission model) — `PhaseExecutor` passes permission declarations to `ControlIRExecutor`
- FP-0017 (`0017-sandboxed-execution.md`) — Component D landed (commit `ddf2d05`): `exec.py`
  now carries a `DeprecationWarning`; `PhaseExecutor` should use `sandboxed_exec` instead of
  the deprecated `exec` op when the extraction lands
