# FP-0003: User approval and resume flow on budget exceed

> **Superseded in part (#1877, 2026-06-20):** the per-dimension
> `CostLimitConfig.ask_on_exceed` bool described below was removed and subsumed
> into the unified `safety.on_limit` 3-mode policy (FP-0005). The exceed flow
> for `per_chain_skill_calls` is now driven by `safety.on_limit.mode`
> (interactive / auto_extend / unattended); `extension_calls > 0` is the
> per-dimension opt-in signal. `extend_chain_calls` + the extension bookkeeping
> are unchanged. Read `ask_on_exceed` below as historical context.

**Status**: done (landed 2026-05-10); `ask_on_exceed` subsumed into `safety.on_limit` (#1877)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Implemented**: 2026-05-10 — `CostLimitConfig.ask_on_exceed` + `extension_calls` fields + `BudgetTracker.extend_chain_calls()` + `ChatSession._ask_budget_extension()` ask_user dispatch + 8 Tier 2 invariants (`tests/test_budget_extend_chain.py`). Backward-compatible: `ask_on_exceed: false` (default) preserves prior hard-refuse behaviour.

---

## Summary

Currently, when the `per_chain_skill_calls` / `per_chain_skill_tokens` hard limit is reached,
skill invocation is immediately rejected with no way to resume.
This proposal adds a mechanism to prompt the user for approval via `ask_user` when the budget is exceeded,
and — if approved — reset the chain's budget and retry the spawn.

---

## Motivation

### Current problem

```
hard_limit reached → skill invocation immediately rejected (return)
                   → error message shown to user
                   → that spawn instance is lost
                   → to resume, the user must manually run /budget reset
                     and re-send the same request from scratch
```

- For long-running multi-agent tasks that hit the budget limit mid-chain,
  all intermediate results accumulated so far are discarded
- `/budget reset` resets the entire counter, which cannot prevent
  unintended impact on other chains
- The user may well judge "this work should continue," yet the system
  forces a hard stop

### Use cases

- Large code-generation tasks where `skill_builder` is called more than expected — prompt mid-way
- Research tasks where `web_search` / `read_local_files` hit the limit — approve to continue
- Using hard limits as a safety net while retaining the flexibility to exceed them when necessary

---

## Proposed implementation

### Flow

```
hard_limit reached
    ↓
ask_user("skill 'X' has reached limit N. Continue for up to M more calls? [yes/no]")
    ↓
/answer yes  →  extend_chain(chain_id) + retry spawn (+M extension)
/answer no   →  same immediate rejection as today (error message)
timeout      →  treated as /answer no (default deny)
```

### Implementation locations

**session.py (budget exceeded path):**

```python
# current
if not check.allowed:
    self._emit_budget_exceeded(...)
    return  # immediate reject

# after change (when ask_on_exceed is enabled)
if not check.allowed:
    if self._should_ask_on_exceed(check):
        approved = await self._ask_budget_approval(chain_id, skill_name, check)
        if approved:
            self._budget.extend_chain(chain_id, skill_name, extension_calls=N)
            # retry spawn
        else:
            self._emit_budget_exceeded(...)
            return
    else:
        self._emit_budget_exceeded(...)
        return
```

**Connection to InterventionBus:**

Reuses the existing `InterventionBus.ask(question)`.
Leverages the same pause / resume infrastructure as the `ask_user` Control IR op.

### Configuration

```yaml
# reyn.yaml
cost:
  per_chain_skill_calls:
    hard_limit: 5
    warn_ratio: 0.8
    ask_on_exceed: true    # new flag (default: false = preserves current behavior)
    extension_calls: 3     # number of additional calls granted on approval
```

`ask_on_exceed: false` (default) preserves current behavior exactly.
Opt-in design — no impact on existing users.

### Extension design

- Uses `extend_chain(chain_id, skill, +N)` rather than `reset_chain()`,
  partially extending the counter only (no impact on other skills or chains)
- Each approval raises the limit by `extension_calls`
- Approval can happen any number of times (ask_user fires on each occurrence)

---

## Dependencies

- `src/reyn/budget/budget.py` — add `extend_chain()` method
- `src/reyn/chat/session.py` — add ask_user hook to budget exceeded path
- `src/reyn/user_intervention.py` / `InterventionBus` — existing, no changes needed
- `src/reyn/config.py` — add `ask_on_exceed`, `extension_calls` to `CostLimitConfig`

Prerequisite PRs: none (can be implemented independently)

---

## Cost estimate

**Total: SMALL**

| Task | Cost | Notes |
|---|---|---|
| Add flags to `CostLimitConfig` | SMALL | add 2 fields only |
| Implement `extend_chain()` method | SMALL | partial counter limit extension |
| Add ask_user to session.py exceeded path | SMALL | one InterventionBus call |
| Default deny on timeout | SMALL | handled via ask_user timeout argument |

No bottlenecks. All tasks are SMALL.

---

## Related

- `src/reyn/budget/budget.py` — BudgetTracker implementation
- `src/reyn/chat/session.py:2554` — current budget exceeded path
- `src/reyn/user_intervention.py` — InterventionBus (ask_user infrastructure)
- FP-0001 (`0001-a2a-task-lifecycle.md`) — proposal to connect the same InterventionBus to A2A
