# B9-S5a Retest — Natural Language eval_builder Routing

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `330dd2a` |
| Verdict | **refuted** |
| B8 baseline | refuted ([B8-S5a](../../2026-05-04-batch-8-cumulative-verify/findings/B8-S5a-eval-builder-natural.md)) |
| Predicted top (B9 prelude) | verified (30%) / refuted (35%) |
| B9 fixes active | G15 + G16 + G17 |

## Setup

- worktree: `agent-a733e8e0a9006229f` (clean, main HEAD `330dd2a`)
- `.reyn/` flushed with `rm -rf`
- `reyn.yaml`: `python.trusted: allow` temporarily added (not committed)
- flag: `--allow-untrusted-python`
- trace dump: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b9_s5a.jsonl`
- input: `direct_llm の eval を作って`
- wall time: ~23s (no timeout)

## Observation

### dogfood_trace --mode summary

```
[Skill Chain]  (8 workflows)
  [T+4s]  eval (run_target → evaluate)  status=finished   ← WRONG SKILL
  [T+5s]  direct_llm (respond)  status=finished          ← eval ran direct_llm as target
  [T+12s] skill_narrator (narrate)  status=finished
  [T+15s] eval (run_target → evaluate)  status=finished   ← 2nd invoke, same wrong skill
  [T+17s] direct_llm (respond)  status=finished
  [T+23s] skill_narrator (narrate)  status=finished

[Tool Calls]  (7 important)
  [ 1] list_skills({"path": ""})
  [ 2] list_skills({"path": "general"})
  [ 3] describe_skill({"name": "eval"})            ← described eval, not eval_builder
  [ 4] invoke_skill({"name": "eval", ...})          ← eval skill invoked (wrong)
  [ 5] run_skill({"skill": "direct_llm", ...})      ← eval ran direct_llm as target
  [ 6] invoke_skill({"name": "eval", ...})          ← router re-invoked eval (2nd time)
  [ 7] run_skill({"skill": "direct_llm", ...})

Cost: $0.001295  |  51,788 tokens  |  13 LLM calls
```

### Router decision trace

```
[T+2s]  list_skills({"path": ""})
[T+3s]  list_skills({"path": "general"})
[T+3s]  describe_skill({"name": "eval"})         ← describe step for eval, not eval_builder
[T+4s]  invoke_skill({"name": "eval",
         "input": {"dsl_root": "", "case_input": "direct_llm の eval を作って",
                   "case_name": "test", "spec_path": "eval.md",
                   "target_skill_path": "direct_llm", "phase_criteria": ""}})
```

The router:
1. Listed skills (2 turns)
2. Described `eval` (not `eval_builder`)
3. Invoked `eval` with hallucinated input fields (`spec_path`, `phase_criteria`, `dsl_root` — these are eval_builder input fields, not eval's)

The `eval` skill accepted the invocation but ran `direct_llm` as target and completed its
own run/evaluate/narrator flow — this is the wrong skill completing a wrong task.

### eval_builder invocation: none

```
eval_builder was never described, never invoked.
eval.md: NOT generated.
```

### G16 wording effect analysis

G16 changed `eval_builder` description to include "Build" verb and `when_not_to_use`
clarifying it differs from the `eval` skill. However:

1. The system prompt listing shows skill descriptions **truncated to ~70 chars**:
   ```
   eval:         Evaluate a target skill against a single test case using judge_phase...
   eval_builder: Build an eval spec (eval.md) — to run evaluations use the eval skill...
   ```
2. The router chose `describe_skill(eval)` after listing, never describing `eval_builder`
3. Input `直llm の eval を作って` — the token `eval` in the input anchors the router
   to the `eval` skill, not `eval_builder`
4. The wording fix (G16) added "Build" verb and when_not_to_use, but the truncated
   listing still doesn't distinguish them sufficiently for weak LLM

### Attractor detection

```
Total LLM calls: 15
Detected attractors: 0 (0%)
```

No attractors. Router does not empty-stop; it incorrectly but confidently routes to `eval`.

### Cost

```
Total: $0.001295  |  51,788 tokens  |  13 LLM calls
```

## Delta vs batch 8

| Item | B8-S5a (8e15019) | B9-S5a (330dd2a) |
|---|---|---|
| Skill invoked | `eval` (wrong) | `eval` (wrong) — unchanged |
| eval_builder invoked | NO | NO |
| eval.md generated | NO | NO |
| Router path | list→list→describe(eval)→invoke(eval) | list→list→describe(eval)→invoke(eval)×2 |
| Hallucinated input fields | eval-style fields | eval-style fields (same pattern) |
| G16 wording effect | N/A | No observable routing change |
| LLM calls | 12 | 15 |
| cost | $0.001246 | $0.001295 |

The routing failure pattern is **identical to B8-S5a**. G16 wording change produced
no observable improvement in the natural-language routing path.

## Verdict reasoning

**refuted**: G16 fix (eval_builder description wording) did not resolve the routing
ambiguity. The router continues to select `eval` skill when the input is `direct_llm の eval を作って`.
The "Build" verb and when_not_to_use clarification are not visible in the truncated
listing (~70 chars), and the router chose to describe only `eval` (not `eval_builder`)
after listing.

The fix addressed the symptom at the wrong level — the semantic disambiguation requires
either (a) `when_to_use` positive matching improvement, (b) dedicated negative keyword
injection for `eval` skill to redirect `作って` intent to `eval_builder`, or (c) router
system prompt guidance that distinguishes "build spec" from "run eval" intent before skill
discovery.

## Implications

### G16 assessment: insufficient wording fix

The G16 wording fix is a necessary but not sufficient condition for correct routing.
The weak LLM (gemini-2.5-flash-lite) anchors on the literal word `eval` in user input
regardless of the skill description wording. A stronger intervention is needed:

Option A: Rename eval_builder's intent keyword to avoid `eval` token collision
Option B: Add explicit negative example in router system prompt: "if user says 'eval を作って' that means eval_builder, NOT eval"
Option C: Add a `routing_hint` field to skill.md that the router system prompt inlines verbatim

### B9-NEW pattern: router intent confusion remains

The natural-language routing of `eval` vs `eval_builder` is a confirmed recurring issue
(B7-S5a, B8-S5a, B9-S5a all refuted). This is a structural problem with LLM semantic
disambiguation, not a wording issue alone. Batch 10 candidate: stronger routing signal
(e.g. exclusive keyword list per skill in router system prompt).
