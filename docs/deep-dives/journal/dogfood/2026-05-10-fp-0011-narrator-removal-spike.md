# FP-0011 Narrator-Removal G4 Spike

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| Spike branch | `claude/fp-0011-narrator-removal-spike` (HEAD: `0e442b4`) |
| Driver | `scripts/dogfood_g4_spike.py` (= main HEAD `955306c`) |
| Scenarios | `narr-1-mcp-search` + `narr-3-skill-builder` |
| Conditions | weak-baseline / weak-experimental / strong-experimental |
| Total runs | 18 (= 2 scenarios × 3 conditions × N=3) |
| Total flash requests | ~30 |
| Cost | ~$0.30 |

## Status: COMPLETE — **FP-0011 is land-recommended (quality improvement)**

## Headline (= corrected after 3-stage context analysis)

**FP-0011 (= narrator skill removal) is a net quality improvement**, not a
regression. Three findings from events-log audit overturned the initial
spike framing:

1. **narrator only fires on success path** (= bypassed on failure / abort);
   only 2 of 18 spike runs invoked narrator. Failure narration has always
   been the router LLM's job.
2. **narrator itself hallucinates** (= shot 1: narrator said "image_captioning
   skill, 4 files"; tool_result said "string_length", router-narration
   correctly said string_length). narrator is not a trustworthy
   ground-truth source — it's another LLM call subject to the same biases.
3. **router LLM uses tool_result as ground truth and overrides narrator's
   output**. The user-visible narration in narrator-on conditions came from
   the router LLM's post-tool turn, not narrator. Removing narrator
   eliminates one (unreliable) output without losing quality.

Recommendation: **land FP-0011 as proposed**. Single observed
flash-tier hallucination (= narr-3 SE shot 2) is unrelated to narrator
removal — it's a model behavior issue when router LLM ignores
`tool_result.status="error"`. Component B SP guidance can be tightened
as a separate hardening task.

## Process retrospective: 3 stages of context analysis

This spike's value is as much in the **observation-discipline correction**
as the data itself. Three reframings happened mid-spike, each triggered by
user pressure on under-verified claims.

### Stage 1: speculation as conclusion (= initial framing)

```
strong-experimental の router LLM が skill 失敗を success と幻覚。
flash で 3/3 全件 hallucinate。 narrator removal は深刻な regression。
```

Evidence: 1 events-log inspection + 17 narration-text observations.

User intervention: **「100% hallucination は events 直接確認した?」**

### Stage 2: per-shot events audit (= partial correction)

Direct audit of all 18 events logs:

| Match | Count |
|---|---|
| TRUTHFUL | 17/18 (= 94%) |
| HALLUCINATED | 1/18 (= 5.6%) |

Re-framing: "FP-0011 land with caveat for flash strong tier (= 1/6
hallucination needs Component B strengthening)".

User intervention: **「narrator の出力と router への入力は一致してた?」**

### Stage 3: narrator vs router output comparison (= final correction)

Discovered that the spike's "narrator on" condition was effectively
testing **router's narration with narrator running in parallel**, not
"narrator's narration". Three sub-findings:

**(a) narrator only fires on success path.** 18-run audit:

| Run group | narrator workflow_started events |
|---|---|
| narr-1 weak-baseline (3, all failure) | 0/3 |
| narr-3 weak-baseline shot 1, 2 (success) | 2/2 |
| narr-3 weak-baseline shot 3 (failure) | 0/1 |
| spike branch all 12 runs (narrator removed) | 0/12 |

`_run_one_skill` bails before `_invoke_narrator` on exception or
budget_exceeded paths. **Failure narration has always been the router
LLM's job, regardless of FP-0011**.

**(b) narrator hallucinates skill names.** From narr-3 weak-baseline shot 1:

- narrator's `reply_text`:
  > 「skill_builder で **image_captioning** スキルを正常に作成しました。
  > **4 つのファイル** が書き込まれました。」
- tool_result data: `{skill_name: "string_length", file_count: ...}`
- user-visible narration (= router's output):
  > 「**string_length** スキルを正常に作成しました... text (string, required)...
  > reyn/local/string_calculator/skill.md ...」

narrator hallucinated the skill name. The router LLM's post-tool turn,
having access to the actual `tool_result.data`, generated correct
narration that ignored / overrode narrator's output.

**(c) router uses tool_result as ground truth + elaborates.** Same shot 1
example shows the router going beyond narrator's terse summary, adding
the input/output spec from `tool_result.data` (= text/length fields,
implementation note, file list).

### Conclusion of Stage 3

FP-0011's "double-output risk" framing in the proposal was understated.
The reality is:

- narrator and router both produce narration on success path
- narrator's output is unreliable (= sometimes hallucinates)
- router's output is grounded in `tool_result.data` (= the actual artifact)
- removing narrator eliminates the unreliable parallel output
- = **net quality improvement, not regression**

## Audit data (= per-shot ground truth)

per-condition truthfulness from events vs narration:

| Condition | TRUTHFUL | HALLUCINATED | Note |
|---|---|---|---|
| weak-baseline (= main, narrator on) | 6/6 | 0/6 | router output measured (narrator runs only on 2/6 successes, but its hallucinated text is overridden by router) |
| weak-experimental (= spike, narrator off, flash-lite) | 6/6 | 0/6 | router single-output, all truthful |
| strong-experimental (= spike, narrator off, flash) | 5/6 | 1/6 | router single-output, 1 case ignored `status="error"` field |

The single hallucination (= narr-3 SE shot 2):
- skill_builder failed with invalid JSON
- `tool_returned.result.status = "error"`,
  `tool_returned.result.data.error = "LLM returned invalid JSON..."`
- router LLM (flash) saw the error directly, narrated success anyway

This is a **flash model behavior issue** (= post-tool turn ignoring
`status="error"`), not narrator-related. Same model would have ignored
the error whether narrator was running or not.

## Audit methodology

For each run, read `spike_results/fp_0011/events/<run_id>.jsonl` directly:

```python
gt = "success" if (workflow_finished > 0
                   and skill_run_failed == 0
                   and workflow_aborted == 0)
     else "failure"
```

ambiguous narration text classified by `candidates: []` content for
narr-1 mcp_search (= "見つかりませんでした" with empty result list = truthful
narration of empty success).

## FP-0011 recommendation

**Land FP-0011 components A + B + C + D + E as proposed.** The spike
data supports the original proposal's claim that router LLM can narrate
skill results inline. The narrator skill is currently both:
- redundant (router elaborates from tool_result anyway)
- unreliable (narrator hallucinates skill names, then router corrects)

Component B (= router SP narration guidance) should be **tightened
beyond the proposal's draft** based on the observed flash hallucination:

```
- After invoke_skill returns: ...
  Status guidance:
    * "finished"             — confirm completion; extract user-relevant fields.
    * "loop_limit_exceeded"  — say the skill ran out of phase budget.
    * "error" / "*" with `data.error` field — your reply MUST surface
      the specific error verbatim. Do NOT summarise as success.
      Quote the error message in user-friendly form. (← new, prevents
      flash strong-tier optimism bias observed in 2026-05-10 spike.)
```

Follow-up: **N≥10 retest on flash strong tier** post-Component-B-strengthen
to confirm hallucination rate drops from 1/6 to ~0.

## Spike infrastructure findings

7 driver-side bugs surfaced + fixed during spike (= material for future
spike infra refinement):

1. CLI `--http-timeout` default 120s overriding driver function default
   360s (`955306c`)
2. Worktree collision when target branch is operator's checkout →
   `--detach` worktree (`955306c`)
3. Stale `reyn web` port collision → `lsof -ti` pre-bind kill (`d44c246`)
4. Server log capture (PIPE buffer / `PYTHONUNBUFFERED`) (`d44c246`)
5. Editable install: `subprocess.Popen(cwd=worktree)` still imports from
   project_root → `PYTHONPATH=<worktree>/src` injection (`d44c246`)
6. History contamination across same-agent runs → per-(scenario, condition,
   shot) unique agent name + `reyn agent rm + new` (`d44c246`)
7. Trust-gate flag missing on `reyn web` → temporary
   `PermissionResolver(trusted_python_allowed=True)` patch on spike
   worktrees (`9bbf2c1`, R-PURE-MODE-REDEFINE pending for structural fix)

## Architectural follow-ups

- **R-PURE-MODE-REDEFINE** (~3-5 day): pure mode formal property
  redefinition (= "ambient sources only" single property) + stdlib
  python I/O refactor to run_ops. See plan file for full detail.
- **R-WEB-TRUSTED-PYTHON** (= initial proposal): wrong-layer fix,
  superseded by R-PURE-MODE-REDEFINE.

## Lessons (= reusable observation discipline patterns)

### 1. Pre-conclusion observation checklist mechanism (= new)

`feedback_pre_conclusion_observation_checklist.md` + CLAUDE.md Tier 1
rule. Trigger words (= 結論 / 100% / 全件 / N/N / pattern / decisive /
attractor / hallucination) fire a 5-question checklist at write time:

1. specific observations enumerated?
2. primary data or inference?
3. falsifying data sought?
4. observation infra supports the claim?
5. N/N directly inspected or extrapolated?

### 2. Cascading stages of correction

This spike went through **3 stages of reframing**, each triggered by
user pressure on under-verified claims:

| Stage | Claim | Verification gap | Trigger |
|---|---|---|---|
| 1 | "100% hallucination on flash" | only 1 events log inspected | "100% は events 直接確認した?" |
| 2 | "5.6% hallucination, FP-0011 risky" | events audited, but narrator role assumed | "narrator output と router input 一致してた?" |
| 3 | "FP-0011 quality improvement" | full architectural model verified | (final) |

Each correction came from asking: **"what's the minimal direct observation
that would falsify or validate this claim?"** and going to look. The
context-analysis trigger mechanism (= layer 1) catches stage 1; layer 2
(= asking about input/output flow) needs explicit thinking about
**system architecture**, not just per-run data.

### 3. Don't confuse **narration produced by component X** with
**narration the user sees**

In a multi-component pipeline (= narrator + router), the user-visible
output may not be from the component you're testing. Always trace the
final-output provenance through the stack before making per-component
quality claims.

### 4. driver bug 7 件 = "primary 1st run is always confounded" reality

Primary spike infrastructure surfaces multiple bugs first run. Budget for
2-3 iterations before data is meaningfully clean.
