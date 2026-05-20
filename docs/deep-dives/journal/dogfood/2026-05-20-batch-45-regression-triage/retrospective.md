# B45 Retrospective — 2026-05-20

**Batch focus**: triage B44 W1/W2/W3 regressions, hold PR #287 + PR #290
ENV flags on for cumulative evidence toward default-flip, exercise the
new batch tooling under a second run.

- HEAD at dispatch: `23a7a1ad` (= B44 PR branch tip; no Reyn src changes
  vs B44 dispatch HEAD `bffe32e4`, only B44 journal additions)
- ENV: `REYN_EMPTY_STOP_RETRY=1`, `REYN_SPAWN_ACK_TO_LLM=1`
- User params (held constant vs B43/B44): `hot_list_n=10`,
  `models.tier=flash-lite`
- Sub-agent model: sonnet (= same as B44 worker dispatch)
- Hard caps observed: 50 tool-uses / 15 min wall-clock per worker

## Headline (with caveats)

| Metric        | B42  | B43  | B44   | **B45**  | ΔvsB44 |
|---------------|------|------|-------|----------|--------|
| V (verified)  | 21   | 22   | 23    | **14**   | **-9** |
| V/N           | 21/54| 22/54| 23/50 | 14/49    |        |
| Verified rate | 0.39 | 0.41 | 0.46  | **0.286**| -0.17  |

**The -9V drop is NOT cleanly attributable to Reyn behavior change.**
See "Confounders" below — measurement-process variables changed between
B44 and B45 in ways that can plausibly explain part or all of the delta.

## Per-worker (V) — past-comparison table

| Worker | Scenario set                | B43 V | B44 V | **B45 V** | ΔvsB44 |
|--------|-----------------------------|-------|-------|-----------|--------|
| W1     | chat_router_smoke           | 3/7   | 2/7   | 1/7       | -1     |
| W2     | stdlib_skills_core          | 5/9   | 3/9   | 1/9       | -2     |
| W3     | control_ir_ops              | 4/9   | 3/9   | 3/9       | 0¹     |
| W4     | permissions_and_safety      | 4/8   | **7/8**| 3/8      | **-4** |
| W5     | multi_agent_and_mcp         | 2/7   | 2/7   | 1/7       | -1     |
| W6     | plan_mode_fp_0011_mixed     | 1/7   | 1/3²  | 2/3       | +1     |
| W7     | long_session_v1             | 3/7   | 5/7   | 3/7       | -2     |

¹ W3 stayed at 3/9 (= identical to B44) = **structural plateau confirmed**
across two consecutive batches, not N=1 noise.
² W6 scenario yaml limits run to 3 (per scenario_set definition).

## OS-side wins (PR #287 + PR #290 evidence accumulation)

Both env-gated paths fired and continued to behave correctly:

**PR #287 (chat-router empty-stop retry)**:
- W6 fired `router_empty_response_retry_injected` × **5 events**
  (S1=2, S3=3). Combined cumulative B44+B45: **8 events** across
  6+ scenarios.
- No false-positive injection observed.

**PR #290 (spawn-ack → LLM, env-gated)**:
- W7 fired `invoke_skill_spawn_ack_exit` × **3 events** (S5 T1/T4/T5).
- Literal `_SPAWN_ACK_MSG` substring scan: **0 echoes** across all
  W7 turns.
- H3 hallucination (= fabricated skill output): **0 cases**.
- Combined cumulative B44+B45: **6 spawn-ack turns / 0 H3 / 0
  literal echoes**. Toward the B45-B47 N≥100 target: **~6% covered**.

Both fixes continue to behave as designed under B45's larger surface.

## Confounders (READ BEFORE INTERPRETING -9V)

Per `feedback_pre_conclusion_observation_checklist`, before calling
this batch a "real regression":

**1. Worker prompt template changed (B44 hand-rolled → B45 generated)**.
The new `dogfood_batch_dispatch.py` template re-wrote setup steps,
verdict rule phrasing, and past-V citation format. Sub-agents (sonnet)
are sensitive to prompt wording when judging V/I/R/B borderline cases.
**Effect on -9V**: unknown but plausible 2-4V.

**2. Main dispatch prompt framed B45 as "regression triage"**. W2/W3/W4/W7
prompts explicitly said *"B44 regressed X→Y, confirm structural vs
noise"*. This is textbook anchoring — sub-agents are primed to find
regressions. **Effect on -9V**: unknown but plausible 1-3V across
the 4 framed workers.

**3. B45 cited B42+B43+B44 baselines in the worker prompt; B44 cited
only B43+B42**. The additional B44 anchor (= "W4 hit 7/8 last batch")
may have biased sub-agents toward stricter pass criteria in B45.
**Effect on -9V**: unknown, small.

**4. LiteLLM proxy was restarted just before B45 dispatch**. Proxy
config file mtime confirms identical content (2026-05-10), so the
restart itself shouldn't change behavior — but upstream Gemini API
may have rolled a model build between B44 (this morning) and B45
(this evening). **Effect on -9V**: unverifiable; treat as bounded
day-to-day LLM variance.

**5. I count rose dramatically: B44 I=8 → B45 I=18 (+10I)**. If the
underlying Reyn behavior were unchanged, this means **10 scenarios
shifted between V/R and I**. That is the shape of a judging-process
change (= partial-credit reclassification), not a uniform code
regression. Some of the V→I or R→I shifts are plausibly the same
behavior judged differently.

In short: **-9V is consistent with both (a) real Reyn regression of
2-5V plus (b) measurement-process drift of 4-7V**. The data does not
let us resolve the split.

## Worker-side new findings (= candidates for B46+ triage)

These are recorded as *signal*, not blockers, given the confounders above.

- **B45-F1 (W1/W5/W7)**: inline routing dominance. Multiple scenarios
  see the router reply inline (= no `routing_decided` event, no
  `direct_llm` skill spawn) while still producing usable text replies.
  Rubrics requiring spawn events fail. **Possible scenario-design
  issue, not OS regression.**
- **B45-F2 (W1 S2)**: `web__search` used for a `direct_llm` factual
  question (hijacking).
- **B45-F3 (W2 S4)**: skill_builder dispatch routed to
  `skill__simple_memo_app` instead. **Routing-decision quality
  candidate.**
- **B45-F4 (W4 S7)**: `hot_list` cross-agent contamination hypothesis
  (= S7 reply mentioned S3 context). Needs trace replay to confirm
  vs reject — currently *worker hypothesis only*, not proven.
- **B45-F5 (W3 S4/S6/S7/S9)**: 4 structural surfaces — `web__fetch`
  permission gap, eval postprocessor schema mismatch,
  `recall_indexed_source` `KeyError('sources')`, `skill_builder`
  invocation failure.

## Carry-overs to B46

1. **Re-run B46 with prompt-process controls** to disambiguate
   regression vs measurement drift:
   - Drop the "Special focus: regression" framing from worker prompts.
   - Cite *only B45* as past-V anchor (single recent point).
   - Same Reyn HEAD, same ENV, same user params, same sub-agent model.
   - If B46 V recovers toward 20-22, the B45 drop was measurement
     drift. If B46 stays at 14-16, the regression is in Reyn behavior
     and individual NF triage starts.
2. **PR #290 default-flip evidence continues**: need ~94 more
   spawn-ack turns over B46+ to reach the N≥100 target.
3. **PR #287 default-flip evidence continues**: 8 cumulative
   `retry_injected` events feels close to "stable behavior"; defer
   formal default-flip-readiness review until after B46.
4. **B45-F1 / B45-F4 are scenario-design hypotheses**, not OS bugs;
   address only if B46 confirms they recur with the new framing.

## Pipeline / tooling note

This batch validated the new tooling end-to-end again under a regression
scenario:
- `dogfood_batch_dispatch.py` cleanly generated 7 worker prompts +
  worktrees + `reyn.local.yaml` overrides (flash-lite forced everywhere,
  no strong-tier invocation observed across 275 LLM round-trips).
- `dogfood_aggregate.py` ingested all 7 worker JSON files cleanly
  including W6's smaller scenario count (3 vs 7).
- Confirmed `LITELLM_API_BASE` env var → `reyn.local.yaml api_base:`
  wiring works (= 275 successful 200 OK requests with content + token
  usage).
