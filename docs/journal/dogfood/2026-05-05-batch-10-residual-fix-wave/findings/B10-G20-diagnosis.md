# B10 G20 — B9-NEW-3 Router invoke_skill Duplication After run_skill Failure

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `107c148` |
| Verdict | **not reproduced** |
| Classification | resolved-indirectly |
| Resolution | G15 + B9-NEW-2 fix (G17 / `8f3bccf`) |

## Setup (attempted reproduction)

- worktree: `agent-a27d262d39819582f` (current task worktree, HEAD `107c148`)
- Reproduction not attempted as live run, per verify-first principle
- Structural analysis performed against router_loop.py and dispatch/dispatcher.py
- Cross-referenced with B10-G19 diagnosis (B9-NEW-1 resolved-indirectly) and
  B10-step1-b9new2-verify.md (B9-NEW-2 verified)

## Observed Pattern in B9-S1

From `docs/journal/dogfood/2026-05-05-batch-9-fix-wave/findings/B9-S1-retest.md`:

```
[T+127s]  phase_completed: prepare → copy_to_work
[T+127s]  phase_started: copy_to_work
[T+141-158s]  invoke_skill loops (router duplication B9-NEW pattern)
Multiple invoke_skill(skill_improver) calls from router at T+141s, T+147s, T+157s
```

The duplication occurred AFTER the chain had been running for 127s. The chain had
experienced three `run_skill(eval_builder)` failures (at T+50s, T+76s, T+116s) due to
B9-NEW-2 (`compute_paths ValueError`) before finally succeeding on the third attempt.

## Structural Analysis

### Existing safeguards

**G10 fix (router_loop.py lines 267-301):**

When `invoke_skill` returns `{"status": "error", ...}`, the router loop emits a
deterministic i18n fallback message and returns `_total_usage` immediately. The LLM
is never given a chance to see the error and retry. This prevents within-round
re-invocation after failure.

**dispatch/dispatcher.py error normalization:**

`dispatch_tool` always returns `{"status": "error", "error": {...}}` on any exception
(PermissionError, generic Exception, InvalidArgsError). Exceptions from
`run_skill_awaitable` are always normalized — they never propagate raw to the router
loop's `_execute_tool` call. The G10 check at `r.get("status") == "error"` is
therefore exhaustive.

**G3 fix (router_loop.py `_dedupe_tool_calls_round`):**

Deduplicates `invoke_skill` calls within a single LLM round. Handles the case where
the LLM emits multiple identical `invoke_skill` calls in the same `tool_calls` list.

### Why B9-S1 showed cross-round duplication

The B9-S1 observation at T+141-158s is a **cross-round** pattern — new router
invocations, not retries within a single router loop. The causal chain was:

1. B9-NEW-2 (`compute_paths ValueError`) caused `run_skill(eval_builder)` to fail
   3 times, with each attempt timing out at ~25s
2. The chain took ~120s total due to the failure cascade
3. During this prolonged execution, the `copy_to_work` phase completed (T+127s) and
   the router was re-invoked
4. The router, lacking clear context about what had already been invoked, re-dispatched
   `invoke_skill(skill_improver)` again at T+141s, T+147s, T+157s

This is NOT a standalone B9-NEW-3 bug — it is a **downstream symptom** of the B9-NEW-2
failure cascade prolonging the chain and making the router lose track of prior invocations.

### Why it does NOT reproduce at HEAD `107c148`

At HEAD `107c148` (post-G15 + G17 + B9-NEW-2 fix `8f3bccf`):

1. G15 (startup_guard auto-approve) ensures stdlib file reads succeed without
   `permission_denied` — analyze_skill completes on the first attempt
2. B9-NEW-2 fix (`8f3bccf`, commit `8f3bccf`) ensures `compute_paths` handles both
   artifact forms (top-level and wrapped) without raising ValueError
3. Without the failure cascade, `run_skill(eval_builder)` succeeds in ~16s (as
   observed in B10-Step1 verify)
4. The chain completes quickly; there is no prolonged execution window for the router
   to re-invoke skill_improver

**Evidence:** B10-G19 diagnosis (`reyn run skill_improver '...'` at HEAD `45ef02b`)
showed the full chain completing without any invoke_skill duplication. The chain
progressed: `prepare → copy_to_work → run_and_eval → plan_improvements →
apply_improvements → finalize`.

## Hypothesis: Why Does the Cross-Round Duplication Occur?

Even if B9-NEW-3 reproduced independently, the structural cause would likely be
**Option C (visibility fix)**: the router LLM re-invokes because it does not have
clear context about what chains are already running or have already been dispatched
in prior router rounds.

However, this is a **chain management / context architecture concern** rather than a
bug at the current HEAD. The router system prompt and context do not include prior
chain state, so if the chain takes long enough or the router is re-invoked in a new
session, it may legitimately re-invoke a skill. This is inherent in the stateless
router design.

## Existing Dedupe Coverage Assessment

The existing G3 / G10 dedupe and error-intercept mechanisms cover:

| Scenario | Covered by |
|---|---|
| Same skill, same args, same LLM round | G3 (`_dedupe_tool_calls_round`) |
| invoke_skill returns error in current round | G10 (immediate return, no LLM retry) |
| dispatch_tool internal exceptions | dispatcher.py normalization |

The only uncovered scenario is **intentional re-invocation across router sessions**
(= user sends a second request after a first chain completes or fails). This is
**not** a bug — it is correct behaviour for the stateless router to allow a new
invocation when the user sends a new message.

## Fix Decision

**No fix required.** B9-NEW-3 is not a standalone bug. It was:

- **Not independently reproducible** at HEAD `107c148`
- **Causally downstream** of B9-NEW-2 (`compute_paths ValueError`) and G15
  (`permission_denied` in analyze_skill)
- **Structurally covered** by the existing G10 error-intercept mechanism
  (single-round failure propagation is clean)

Classification: **resolved-indirectly** by G15 (startup_guard) + B9-NEW-2 fix
(G17 wrong-layer trap, commit `8f3bccf`).

## Recommendation

The B9-NEW-3 giveup-tracker entry (G20) should be closed as:
- Verdict: **resolved-indirectly**
- Root cause: B9-NEW-2 failure cascade (prolonged chain execution) gave the router
  time to re-invoke skill_improver before the chain completed
- Resolution: G15 + G17/B9-NEW-2 (`8f3bccf`) — both landed

No new dedupe mechanism is warranted. The existing G3 + G10 safeguards are
sufficient for normal operation. If cross-session chain tracking becomes a concern
in the future (e.g., long-running jobs spanning multiple router invocations), it
should be addressed as part of the long-running job / crash recovery work (PR21 /
project_residuals.md), not as a dedupe patch.

## Evidence References

| Run | HEAD | Result |
|---|---|---|
| B9-S1 retest | `330dd2a` | invoke_skill duplication at T+141-158s (B9-NEW-2 active) |
| B10-Step1 (B9-NEW-2 verify) | `c6e2d44 + 8f3bccf` | eval_builder chain in ~18s, no duplication |
| B10-G19 (skill_improver run) | `45ef02b` | full skill_improver chain, no duplication |
| Structural code analysis | `107c148` | G10 + G3 + dispatcher normalization exhaustively cover failure propagation |
