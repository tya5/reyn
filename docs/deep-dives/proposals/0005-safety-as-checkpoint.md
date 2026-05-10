# FP-0005: Treating Safety Limits as Checkpoints — Integration with the Permission Model

**Status**: done (= Phase 1 + Phase 2 landed)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Phase 1 implemented**: 2026-05-10 — `OnLimitConfig` (`mode` / `auto_extend_times` / `ask_timeout_seconds`) added to `safety:` section; `RunResult.partial_data` field landed; abort paths in `OSRuntime.run()` populate `partial_data` on `loop_limit_exceeded` / `phase_budget_exceeded` / `budget_exceeded`. **Default mode = `unattended`** preserves legacy abort-immediately behaviour byte-for-byte; opt into `interactive` / `auto_extend` is explicit. 8 Tier 2 invariants in `tests/test_safety_on_limit.py`.
**Phase 2 implemented**: 2026-05-10 — Shared `handle_limit_exceeded` helper (`src/reyn/safety/limit_handler.py`) + `LimitDecision` dataclass; per-site wiring at all 6 abort paths (B max_phase_visits / F phase_seconds / A max_act_turns in OSRuntime; C router_cap / E max_hop_depth / G chain_seconds in ChatSession); FP-0003's `_ask_budget_extension` (D per_chain_skill_calls) generalised to call the shared helper; CLI factories (chat / web / mcp) thread `config.safety.on_limit` through to ChatSession. 11 helper invariants (`tests/test_safety_limit_handler.py`) + 6 wiring invariants (`tests/test_safety_phase2_wiring.py`). The `_chains.get(chain_id)` peek-before-pop pattern lets the chain_seconds watchdog re-arm on user approval without losing the pending entry.

---

## Summary

All current safety limits (loop detection, timeout, budget exceeded) are implemented as "abort = artifacts lost." The WAL already has the infrastructure to preserve state, and the Permission model already has the "pause → ask → resume/abort" pattern. By integrating both, this proposal treats a limit being reached as a "checkpoint" rather than a "crash," allowing users to decide whether to continue without losing their work.

---

## Motivation

### What users actually need

```
Current behavior: limit reached → abort → LLM costs and artifacts so far are gone
Desired behavior: limit reached → notify → artifacts so far are preserved
                                → user decides whether to continue or stop
```

Expecting users to pre-configure all limits correctly is a worse user experience than **letting them run and interact when something hits a limit**.

### WAL already has the infrastructure

The reason only H (LLM timeout) can currently be resumed is that phase state is saved to the WAL. For all other limits, simply "committing the WAL before aborting" is enough to preserve artifacts. At present, WAL commit is not guaranteed on all abort paths.

### Symmetry with the Permission model

```
No file-write permission  → ask_user → approved → continue
No MCP tool permission    → ask_user → approved → continue
↓ same pattern
loop limit reached        → ask_user → approved → extend limit and continue
timeout limit reached     → ask_user → approved → extend deadline and continue
```

---

## Proposed implementation

### Core changes: 3 steps

**Step A — Commit the WAL when a limit is reached**

On every limit abort path, write the completed steps of the current phase to the WAL before raising the exception. This preserves "artifacts accumulated so far."

```python
# Before
raise LoopLimitExceededError(...)

# After
await self._flush_wal_checkpoint()   # commit WAL
raise LoopLimitExceededError(...)
```

**Step B — Insert an ask_user hook**

Extend the same mechanism proposed for budget exceeded in FP-0003 to all limits.

```python
async def _handle_limit_exceeded(self, exc, kind: str):
    await self._flush_wal_checkpoint()
    if self._limit_mode == "interactive":
        approved = await self._ask_limit_approval(kind, exc)
        if approved:
            self._extend_limit(kind)
            return  # continue
    raise exc  # abort (unattended / denied)
```

**Step C — Switch behavior via execution mode**

```yaml
# reyn.yaml
safety:
  on_limit:
    mode: interactive   # interactive / unattended / auto-extend
    # interactive:   confirm via ask_user (default for reyn chat)
    # unattended:    abort immediately (default for reyn run, CI)
    # auto-extend:   extend automatically N times (for trusted long-running tasks)
    auto_extend_times: 1  # number of automatic extensions when auto-extend
    ask_timeout_seconds: 60  # ask timeout for interactive mode (abort if exceeded)
```

`reyn run` defaults to `mode: unattended` (preserving existing behavior).
`reyn chat` defaults to `mode: interactive`.

### Applicability per limit

| Mechanism | WAL commit | ask_user | Rationale |
|---|---|---|---|
| A. max_act_turns | ✅ | ✅ | Completed ops within the phase can be preserved mid-phase |
| B. max_phase_visits | ✅ | ✅ | The previous phase's completed state is already in the WAL |
| C. router_cap | ✅ | ✅ | Within a turn, so ask is possible before retrying |
| D. per_chain_skill_calls | ✅ | ✅ | Before invocation, so WAL commit is immediate |
| E. max_hop_depth | — | ✅ | Delegation is rejected; the caller is still running, so ask is possible |
| F. phase_seconds | ✅ | ✅ | Can continue by extending the elapsed time limit |
| G. chain_seconds | ✅ | ✅ | Can continue by extending the chain timeout |
| H. llm_timeout + retries | existing | — | Already has automatic retry; ask not needed |

### Returning "artifacts accumulated so far"

Even on abort (user answered no / unattended), WAL-committed phase output is returned as `RunResult.partial_data`.

```python
class RunResult:
    status: str          # e.g. "loop_limit_exceeded"
    data: dict | None    # final output on successful completion
    partial_data: dict | None  # new: partial artifacts on limit abort
    error: str | None
```

Users can inspect this `partial_data` via `/list` or the TUI.

---

## Relationship to FP-0003 / FP-0004

| FP | Relationship |
|---|---|
| FP-0003 (ask_user on budget exceeded) | A per-limit implementation of D (per_chain_skill_calls) in this FP. Merges into Step B if this FP is accepted. |
| FP-0004 (safety config UX improvement) | `safety.on_limit.mode` from this FP is added to the `safety:` section from FP-0004. The two are mutually complementary. |

---

## Dependencies

- `src/reyn/kernel/runtime.py` — `_flush_wal_checkpoint()` + hooks into limit abort paths
- `src/reyn/chat/session.py` — `_ask_limit_approval()` + mode determination
- `src/reyn/user_intervention.py` / `InterventionBus` — existing, no changes needed
- `src/reyn/schemas/models.py` — Add `RunResult.partial_data` field
- `src/reyn/config.py` — Add `safety.on_limit` configuration
- `src/reyn/chat/services/chain_manager.py` — ask hook for G (chain timeout)

Prerequisite PRs: none. However, concurrent implementation with FP-0004 (`safety:` section) is preferred.

---

## Cost estimate

**Total: LARGE**

| Task | Cost | Notes |
|---|---|---|
| Step A: insert WAL commit on all limit abort paths | MEDIUM | 8 sites; each path must be reviewed carefully |
| Step B: `_ask_limit_approval()` shared implementation | SMALL | Consolidate InterventionBus calls |
| Step B: insert ask hooks for each limit | MEDIUM | Each limit has different behavior requiring individual handling |
| Step C: `on_limit.mode` config and default switching | SMALL | config + CLI flag |
| `RunResult.partial_data` addition + return logic | SMALL | Field addition and abort-path return changes |
| Tests (Tier 1 / Tier 2) | MEDIUM | Cover behavioral changes for each limit with contract tests |

The bottleneck is **Step A WAL commit guarantee** (existing abort paths are diverse) and **testing** (limit behavior contracts increase in count).

---

## Related

- `src/reyn/kernel/runtime.py` — Current limit abort paths
- `src/reyn/events/state_log.py` — WAL implementation
- `src/reyn/user_intervention.py` — InterventionBus
- FP-0003 (`0003-budget-exceed-user-approval.md`) — Predecessor to this FP (D-only version)
- FP-0004 (`0004-safety-config-ux.md`) — `safety:` section design (target for integration with this FP)
- `docs/concepts/events.md` — P6 event design
- `docs/guide/for-skill-authors/crash-recovery-and-resume.md` — WAL + forward-replay
