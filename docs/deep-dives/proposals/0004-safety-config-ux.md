# FP-0004: Safety Configuration UX Improvement — Alignment with the Conceptual Layer

**Status**: done (landed 2026-05-10)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Implemented**: 2026-05-10 — `SafetyConfig` + `LoopConfig` + `TimeoutConfig` dataclasses (`src/reyn/config.py`) + `hint_config_key` on `LoopLimitExceededError` / `PhaseBudgetExceededError` / `RouterCapExceeded` + chain-timeout / max-hop-depth error message hints + `docs/guide/for-skill-authors/understand-why-reyn-stops.md` (en+ja) + 11 Tier 2 invariants (`tests/test_safety_config.py`). Originally landed with a deprecation-aware reader that back-filled the legacy `limits:` / `multi_agent:` / `cost.router_invocations_per_turn` / `cost.per_chain_skill_calls.hard_limit` keys; this layer was subsequently removed in `0b464ab` and `safety:` is now the single source of truth (= no migration path; `0.x` configs that still use the legacy keys must be hand-edited).

---

## Summary

The current loop-detection and timeout settings are scattered across three sections — `limits:`, `cost:`, and `multi_agent:` — making it difficult for users to understand why execution stopped and what to change to resume it. This proposal consolidates them into a unified `safety:` section that matches the user's mental model (stop reasons fall into three categories: loop / timeout / budget exceeded) and aligns error messages and documentation accordingly.

---

## Motivation

### Current problems

**Configuration keys spread across 3 namespaces:**

```yaml
limits:
  phase:  max_visits / max_wall_seconds
  llm:    timeout / max_retries
cost:
  router_invocations_per_turn
  per_chain_skill_calls          # ← semantically a "loop guard" but placed under cost:
multi_agent:
  max_hop_depth / chain_timeout_seconds
# + max_act_turns in phase frontmatter
```

**Error messages do not include configuration keys:**

```
[loop_limit_exceeded]
→ Users have no way of knowing which key to change to continue
```

**No conceptual model for "why does Reyn stop" in the documentation:**

10 mechanisms are each described individually, making it impossible for users to build a mental model.

---

## Proposed implementation

### Step 1 — Include configuration keys in error messages (SMALL)

```
[Loop guard] Phase 'revise' has reached its limit of 25 visits.
→ To continue, raise safety.loop.max_phase_visits.

[Timeout] LLM call exceeded 60 seconds.
→ Raise safety.timeout.llm_call_seconds or switch to a different model.

[Loop guard] The router was invoked 3 times in this turn.
→ Raise safety.loop.max_router_calls_per_turn (0 = unlimited).
```

This is a simple change — attach `hint_config_key` at each raise / return site. Existing behavior is unchanged.

### Step 2 — Consolidate into a `safety:` section (MEDIUM)

New configuration schema aligned with the conceptual layer:

```yaml
safety:

  # ① Loop detection — when the same thing is happening repeatedly
  loop:
    max_act_turns_per_phase: 10    # within a phase (LLM ↔ op exchanges)
    max_phase_visits: 25           # across phases (repeated transitions)
    max_router_calls_per_turn: 3   # skill invocations (within a single turn)
    max_agent_hops: 3              # delegation chain depth (A→B→C)
    max_skill_calls_per_chain: 5   # skill invocations (across the full chain)

  # ② Timeout — when something is taking too long
  timeout:
    llm_call_seconds: 60           # a single LLM API call
    llm_max_retries: 3             # retry limit for transient LLM errors
    phase_seconds: 0               # entire phase (0 = unlimited)
    chain_seconds: 60              # multi-agent chain

# ③ Budget exceeded — remains under cost: section
# (token / USD / daily / monthly are financial settings and should stay separate)
cost:
  per_agent_tokens: ...
  daily_cost_usd: ...
  ...
```

**Migration strategy (backward compatible):**

- Old keys (e.g. `limits.phase.max_visits`) are deprecated but continue to be read
- New keys take precedence when both are present
- Old keys will be removed in the next major version

**Moving `per_chain_skill_calls`:**

`per_chain_skill_calls` currently sits under `cost:` but its role is loop detection (a count limit), not financial tracking. It will be moved to `safety.loop.max_skill_calls_per_chain`. Leaving it under `cost:` is misleading since it carries no monetary meaning.

**Handling `max_act_turns`:**

Currently written as `max_act_turns: 10` in phase frontmatter. A global default will be added as `safety.loop.max_act_turns_per_phase`, while per-phase overrides in frontmatter remain supported.

### Step 3 — Create a conceptual document (SMALL)

Add `understand-why-reyn-stops.md` under `docs/guide/for-skill-authors/` or `docs/guide/for-reyn-developers/`:

```
# Why Reyn Stops

There are 3 reasons Reyn stops:
  ① Loop detection  → safety.loop.*
  ② Timeout         → safety.timeout.*
  ③ Budget exceeded → cost.*

For each category:
  - What is happening (with examples)
  - The corresponding error message
  - Which configuration key to change
  - Recommended values and caveats
```

---

## Dependencies

- `src/reyn/config.py` — Add `SafetyConfig`, `LoopConfig`, and `TimeoutConfig` dataclasses
- `src/reyn/kernel/runtime.py` — Attach `hint_config_key` to error messages
- `src/reyn/chat/session.py` — Improve router cap / chain error messages
- `src/reyn/chat/services/chain_manager.py` — Improve chain timeout error messages
- `src/reyn/chat/services/budget_gateway.py` — Migrate `per_chain_skill_calls` to the new key
- `docs/reference/config/reyn-yaml.md` — Update configuration reference

Prerequisite PRs: none (can be implemented independently; Steps 1 → 2 → 3 can each be released independently)

---

## Cost estimate

**Total: MEDIUM**

| Task | Cost | Notes |
|---|---|---|
| Step 1: error message improvements | SMALL | String addition at each raise site only |
| Step 2: `SafetyConfig` dataclass definition | SMALL | Add types to config.py |
| Step 2: old-key → new-key migration layer | SMALL | Deprecated-read logic |
| Step 2: update all references to use new keys | MEDIUM | runtime / session / chain_manager etc., multiple files |
| Step 3: conceptual document | SMALL | One new .md file |

The bottleneck is **Step 2 reference migration** (scattered across multiple modules).

---

## Related

- `src/reyn/config.py` — Current `CostConfig` / `LimitsConfig` / `MultiAgentConfig`
- `src/reyn/kernel/runtime.py` — `LoopLimitExceededError`, `PhaseBudgetExceededError`
- `src/reyn/chat/services/budget_gateway.py` — `RouterCapExceeded`
- FP-0003 (`0003-budget-exceed-user-approval.md`) — ask_user integration on budget exceeded (`safety.loop.max_skill_calls_per_chain` is also in scope)
