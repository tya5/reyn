# B9-S5b Retest — Structured eval_builder Invoke (target_skill=direct_llm)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `330dd2a` |
| Verdict | **refuted** |
| B8 baseline | refuted ([B8-S5b](../../2026-05-04-batch-8-cumulative-verify/findings/B8-S5b-eval-builder-structured.md)) |
| Predicted top (B9 prelude) | verified (35%) / blocked (35%) |
| B9 fixes active | G15 + G16 + G17 |

## Setup

- worktree: `agent-a733e8e0a9006229f` (clean, main HEAD `330dd2a`)
- `.reyn/` flushed with `rm -rf` per attempt
- `reyn.yaml`: `python.trusted: allow` temporarily added (not committed)
- flag: `--allow-untrusted-python`
- trace dump: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b9_s5b.jsonl`
- input: `eval_builder で direct_llm を analyze して、 target_skill=direct_llm`
- 3 attempts required (LLM non-determinism); 3rd attempt reached analyze_skill

## Observation

### Attempt 1 — G12 attractor (empty stop after describe_skill)

```
[T+2s]  list_skills({"path": ""})
[T+3s]  list_skills({"path": "general"})
[T+4s]  describe_skill({"name": "eval_builder"})   ← correct skill described ✅
[T+?s]  finish=stop, completion_tokens=0            ← G12 attractor ❌
```

Router correctly identified `eval_builder` but hit the G12 attractor pattern:
`describe_skill` was called but `invoke_skill` was never emitted. Empty stop with
completion_tokens=0 while MUST rules (`After describe_skill, you MUST call invoke_skill`)
were present in system prompt.

Attractor detection:
```
Total LLM calls: 4
Detected attractors: 1 (25%)
  [T+3.9s  router] stop_with_must_rule
    MUST rule: "After describe_skill, you MUST call invoke_skill or explain in text"
    Response: finish=stop, completion_tokens=0
```

### Attempt 2 — Router clarification (no tool use)

```
[T+2s]  finish=stop, tool_calls=0
Content: "I see you mentioned `direct_llm` along with `eval_builder`. Could you
          please clarify what you'd like to do?"
```

Router responded with a clarification question without calling any tools. 1 LLM call.

### Attempt 3 (decisive) — analyze_skill reached, G17 tested

```
[T+2s]  invoke_skill({"name": "eval_builder", "input": {"eval_spec": {...}, "target_skill": "direct_llm"}})
        + invoke_skill × 3 duplicates (G3 deduplication issue)
[T+2s]  workflow_started: eval_builder (× 3 deduped)
  [T+2s]  phase_started: analyze_skill (× 3)
  [T+4s]  preprocessor_step_failed (× 3)
  [T+4s]  skill_run_failed (× 3)
```

**Router correctly invoked `eval_builder`** ✅ — skill name disambiguation works when explicit name
is in input. However 4 parallel invocations (deduplication issue).

**G17 fix failure confirmed:**

```
python_step_failed:
  module: ./analyze_skill_resolver.py
  function: compute_paths
  kind: ValueError
  error: "Cannot extract skill name from user_message text: ''. Please use the form..."
```

Input artifact stored on disk:
```json
{"eval_spec": {"name": "direct_llm.md"}, "target_skill": "direct_llm"}
```

Root cause of G17 failure:
- The artifact stored is `{"eval_spec": ..., "target_skill": "direct_llm"}` — NO `data` wrapper
- G17 fix: `data = artifact.get("data", {})` → `data = {}` (artifact has no `data` key)
- `"target_skill" in data` → `False` (target_skill is at top level, not in `data`)
- Falls through to text fallback: `data.get("text", "") == ""` → ValueError

**G17 fix has a wrong assumption about artifact structure.** The unit tests in
`test_eval_builder_path_resolution.py` test the wrapped form
`{"type": "unknown", "data": {"target_skill": "direct_llm"}}`, but the OS passes
the raw input without a `data` wrapper: `{"eval_spec": ..., "target_skill": "direct_llm"}`.

This is **B9-NEW-2** — G17 fix is logically correct but operates at the wrong layer.

### Artifact structure discrepancy

| Layer | Artifact form |
|---|---|
| Test assumption (G17) | `{"type": "unknown", "data": {"target_skill": "direct_llm"}}` |
| OS actual (runtime) | `{"eval_spec": {"name": "..."}, "target_skill": "direct_llm"}` |
| G17 code: `data = artifact.get("data", {})` | `{}` (wrong — `data` key absent) |

The correct fix is to check `"target_skill" in artifact` (top-level) BEFORE falling
back to `artifact.get("data", {})`, or to check both levels.

### Phase progression (3rd attempt)

| Phase | Reached | Result |
|---|---|---|
| router | ✅ | invoke_skill(eval_builder) — direct (skill name explicit) |
| analyze_skill | ✅ | Preprocessor step[0] fails: G17 fix wrong layer |
| write_eval | ❌ | Not reached |
| eval.md | ❌ | Not generated |

### Attractor detection (3rd attempt)

```
Total LLM calls: 2
Detected attractors: 0 (0%)
```

No attractors in 3rd attempt (router went direct to invoke_skill).

### Cost

```
Attempt 3: $0.000519  |  4,504 tokens  |  2 LLM calls
All 3 attempts combined: ~$0.001700
```

## Delta vs batch 8

| Item | B8-S5b (8e15019) | B9-S5b (330dd2a) |
|---|---|---|
| Router skill selection | eval_builder ✅ | eval_builder ✅ (non-deterministic) |
| analyze_skill reached | ✅ | ✅ |
| G17 compute_paths | ValueError (type mismatch) | ValueError (layer mismatch — same error) |
| Artifact type | unknown | unknown |
| G17 fix layer | artifact.type check | artifact.data check (wrong layer) |
| eval.md generated | NO | NO |

G17 fix did NOT resolve the ValueError. The error is identical but for a different root
cause than the fix assumed.

## Verdict reasoning

**refuted**: G17 fix (`_extract_skill_name` checking `"target_skill" in data`) did not
resolve the ValueError. The OS passes the artifact without a `data` wrapper — the fix
should check `"target_skill" in artifact` (top-level lookup). The unit tests validate the
fix with the wrapped form `{"type": "unknown", "data": {...}}` but the runtime produces
the unwrapped form `{"eval_spec": ..., "target_skill": "..."}`.

Additionally, the run required 3 non-deterministic attempts to reach `invoke_skill(eval_builder)`:
- Attempt 1: G12 attractor (empty stop after describe_skill)
- Attempt 2: Router clarification without tool use
- Attempt 3: Direct invoke_skill (correct)

Even with invoke_skill succeeding, the preprocessor immediately fails due to G17 wrong layer.

## Implications

### B9-NEW-2: G17 fix wrong layer (artifact structure mismatch)

The G17 fix must be corrected to handle the actual runtime artifact structure. The
`_extract_skill_name` function should check `"target_skill"` at the artifact top level
first (the actual OS output), falling back to `data["target_skill"]` for legacy or
typed form:

```python
# Check top-level first (OS runtime form: no data wrapper)
if "target_skill" in artifact:
    name = str(artifact["target_skill"]).strip()
    if name:
        return name

# Check nested data form (typed eval_builder_request / legacy)
data = artifact.get("data", {})
if "target_skill" in data:
    name = str(data["target_skill"]).strip()
    if name:
        return name
```

This is a Tier 2 fix (runtime contract correction), not a logic change.

### G12 attractor still present in S5b

Attempt 1 of S5b triggered the G12 `stop_with_must_rule` attractor again. This confirms
G12 is a recurring non-deterministic issue in the router for this scenario. G18 (router
tool function description truncation) remains a relevant fix candidate for batch 10.

### eval_builder routing non-determinism

In 3 attempts, the router showed 3 different behaviors: G12 attractor, clarification
question, and correct invoke. This high variance with explicit skill name in input is
concerning and may require batch 10 investigation.
