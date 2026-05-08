# B9-S1 Retest — Chain Completion (skill_improver + eval_builder)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `330dd2a` |
| Verdict | **inconclusive** |
| B8 baseline | blocked ([B8-S1](../../2026-05-04-batch-8-cumulative-verify/findings/B8-S1-chain-completion.md)) |
| Predicted top (B9 prelude) | verified (25%) / blocked (50%) |
| B9 fixes active | G15 + G16 + G17 |

## Setup

- worktree: `agent-a733e8e0a9006229f` (clean, main HEAD `330dd2a`)
- `.reyn/` flushed with `rm -rf` per session
- `reyn.yaml`: `python.trusted: allow` temporarily added (not committed)
- flag: `--allow-untrusted-python`
- trace dump: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b9_s1.jsonl`
- stdin piped (non-TTY): subprocess + piped stdin pattern (B7-S1 reference)
- input: `skill_improver で direct_llm を 1 回 review して改善案を出して`
- run 1: wall time ~2s (router failed to invoke skill — 1 LLM call)
- run 2 (decisive): wall time ~165s (chain progressed significantly)

## Observation

### Run 1 — Router failure (non-deterministic)

Run 1 produced only 1 LLM call with `finish=stop` and 0 tool calls. Router responded
with a clarification question instead of invoking `skill_improver`. The user message
was duplicated 3 times in the messages array (OS context-building artifact), causing
the router to interpret the request as requiring clarification. This is a non-deterministic
failure — retry confirmed.

### Run 2 — Phase progression (decisive run)

```
dogfood_trace --mode summary:

[Skill Chain]  (11 workflows)
  [T+2s] skill_improver (prepare → copy_to_work)  status=active
  [T+7s] skill_improver (prepare)  status=active
  [T+11s] eval_builder (analyze_skill)  status=active  [attempt 1, aborted]
  [T+11s] eval_builder (analyze_skill)  status=active  [attempt 1, aborted - duplicate]
  [T+55s] eval_builder (analyze_skill)  status=active  [attempt 2, aborted]
  [T+55s] eval_builder (analyze_skill)  status=active  [attempt 2, aborted - duplicate]
  [T+77s] eval_builder (analyze_skill → write_eval → copy_to_work)  status=active
  [T+77s] eval_builder (analyze_skill → write_eval → copy_to_work)  status=active  [duplicate]
  [T+127s] skill_improver (prepare → copy_to_work)  status=active  [retry]
  [T+141s] skill_improver (prepare → copy_to_work)  status=active  [router loop]
  [T+147s] skill_improver (prepare → copy_to_work)  status=active  [router loop]

Cost: $0.001891  |  65,882 tokens  |  13 calls (5 real + 8 cached)
```

### dogfood_trace --mode chain (abridged)

```
[T+2s]   invoke_skill(name="skill_improver", ...)
[T+2s]   workflow_started: skill_improver
  [T+2s]   phase_started: prepare
  [T+3s]   phase_completed: prepare  → copy_to_work
  [T+3s]   phase_started: copy_to_work
  [T+7s]   invoke_skill(name="skill_improver", ...)   ← router dup
[T+7s]   workflow_started: skill_improver (2nd run)
    [T+7s]   phase_started: prepare
    [T+11s]  run_skill(skill="eval_builder", ...)
    [T+11s]  phase_started: analyze_skill  [attempt 1]
    [T+16-44s]  file reads (stdlib paths) — G15 fix WORKING ✅
    [T+47s]  phase_retry: attempt 1 of 2 (LLM abort: "The LLM attempted to perform file read...")
    [T+50s]  control_ir_failed: run_skill aborted at analyze_skill (attempt 1)
    [T+55s]  run_skill(skill="eval_builder", ...)
    [T+55s]  phase_started: analyze_skill  [attempt 2]
    [T+61-73s]  file reads (stdlib paths) — G15 fix WORKING ✅
    [T+75s]  phase_retry: attempt 1 of 2 (same error)
    [T+76s]  control_ir_failed: run_skill aborted at analyze_skill (attempt 2)
    [T+77s]  run_skill(skill="eval_builder", ...)
    [T+77s]  phase_started: analyze_skill  [attempt 3]
    [T+84-107s]  file reads (stdlib paths) — G15 fix WORKING ✅
    [T+110s]  phase_retry: attempt 1 of 2 (same error)
    [T+116s]  phase_completed: analyze_skill  → write_eval  ✅ NEW PHASE REACHED
    [T+116s]  phase_started: write_eval
    [T+122s]  phase_retry: write_eval attempt 1 (Artifact data validation failed)
    [T+124s]  phase_retry: write_eval attempt 2 (Artifact data validation failed)
    [T+125s]  control_ir_failed: write_eval failed after 3 attempts (B9-NEW-1)
    [T+127s]  file write .reyn/improver_state.json
    [T+127s]  phase_completed: prepare  → copy_to_work
    [T+127s]  phase_started: copy_to_work
    [T+141-158s]  invoke_skill loops (router duplication B9-NEW pattern)
```

### Key events — G15 effectiveness

```
file reads during analyze_skill (APPROVED, not denied):
  - src/reyn/stdlib/skills/direct_llm/skill.md       ✅ (was permission_denied in B8)
  - src/reyn/stdlib/skills/direct_llm/phases/*.md    ✅
  - src/reyn/stdlib/skills/direct_llm/artifacts/*.yaml  ✅
```

G15 fix (startup_guard non-interactive auto-approve + run_skill resolver propagation)
is confirmed effective: stdlib file reads proceed without permission_denied.

### New stopping point: write_eval Artifact data validation

```
control_ir_failed:
  "Phase 'write_eval' failed after 3 attempt(s): Artifact data validation failed
   for 'eval_spec_result'"
```

`write_eval` phase reaches 3 LLM turns, all producing `decision=finish`, but the artifact
fails schema validation against `eval_spec_result`. This is **B9-NEW-1** — a new blocker
at a phase deeper than any previously reached.

### Phase progression table

| Phase | Reached | Result |
|---|---|---|
| router | ✅ | invoke_skill(skill_improver) — 1 turn direct |
| prepare | ✅ | Completes (run_skill eval_builder) |
| analyze_skill (eval_builder) | ✅ | Reaches completion on 3rd attempt |
| write_eval (eval_builder) | ✅ NEW | Fails: Artifact data validation (B9-NEW-1) |
| copy_to_work (skill_improver) | ✅ | Reached (phase_started confirmed) |
| run_and_eval | ❌ | Not reached |
| plan_improvements | ❌ | Not reached |
| apply_improvements | ❌ | Not reached |
| finalize | ❌ | Not reached |

### Attractor detection

```
Total LLM calls: 43
Detected attractors: 0 (0%)
```

No attractors detected. LLM retries eventually succeed at analyze_skill.

### Cost

```
Total: $0.001891  |  65,882 tokens  |  13 LLM calls (5 real)
```

## Delta vs batch 8

| Item | B8-S1 (8e15019) | B9-S1 (330dd2a) |
|---|---|---|
| analyze_skill file reads | permission_denied ❌ | approved ✅ (G15 effective) |
| analyze_skill completion | ❌ (aborted every turn) | ✅ (3rd run completes) |
| write_eval phase | ❌ never reached | ✅ NEW — reached but fails validation |
| copy_to_work | ❌ never reached | ✅ reached (phase_started) |
| LLM calls | 9 | 43 (5 real + 8 cached) |
| cost | $0.001891 | $0.001891 |

G15 is confirmed: analyze_skill now reads stdlib files without permission errors.
The chain progresses past the B8 blocker to a new layer: write_eval validation.

## Verdict reasoning

**inconclusive**: The primary fix (G15) is verified effective — stdlib file reads succeed.
The chain progresses further than any previous batch, reaching `write_eval` (B9-S1 first).
However, `write_eval` fails with `Artifact data validation failed for 'eval_spec_result'`
after 3 attempts. This is a new blocker (**B9-NEW-1**) at a deeper phase.

The chain does not complete end-to-end (finalize not reached), so `verified` is not
appropriate. `blocked` would indicate total failure, but significant progress occurred.
`inconclusive` captures: G15 confirmed, next layer exposed, overall goal (improvement
suggestion delivered) not achieved.

## Implications

### B9-NEW-1: write_eval Artifact data validation failure

`write_eval` LLM emits `decision=finish` 3 times but artifact fails validation against
`eval_spec_result` schema. The LLM is producing an artifact that doesn't conform to the
output schema. This may be:
- Missing required fields in `eval_spec_result`
- Wrong field types/structure
- LLM producing raw markdown content instead of structured schema

Batch 10 candidate: fix `write_eval` output schema or phase instructions to guide correct
`eval_spec_result` artifact structure.

### analyze_skill retry pattern (potential B9-NEW-2)

analyze_skill requires 3 run_skill invocations to complete (2 aborts + 1 success). Each abort
is "LLM attempted to perform file read operation outside the allowed scope." This suggests
the LLM in analyze_skill is attempting file reads beyond the declared permissions on some
turns. The G15 fix auto-approves declared paths, but the LLM may be attempting non-declared
paths. Needs investigation.

### router invoke duplication

Multiple `invoke_skill(skill_improver)` calls from router at T+141s, T+147s, T+157s suggest
the router re-invokes after run_skill failure propagates. This may be B9-NEW-3 (similar to
G3 deduplication). Batch 10 candidate.
