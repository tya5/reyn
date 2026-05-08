# B11-R2: G12 attractor diagnosis — Pattern D (describe_skill verbosity)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD at dispatch | `4898ef9` |
| Worktree | `agent-aac6052f4e223b437` |
| Trace files used | `B10-S5b trace` (reconstructed from inspection), `synthetic B11-R2 trace`, `hypothesis B patch on B10 trace` |
| Verdict | **Pattern D confirmed — structural fix applied** |

---

## Reproduction

**Rate at HEAD `4898ef9`**: 5/10 (50%) empty-stop G12 attractor.

Source: N=10 replay of the B10-S5b trace (`9c4373af` request_id, B10 sandbox `.reyn/`).
The B10 trace captured the exact payload: `list_skills("") → list_skills("general") →
describe_skill("eval_builder") → finish=stop, completion_tokens=0`.

```
=== N-shot replay (n=10) ===
Finish reasons:
  tool_calls: 5
  stop: 5
```

The 5 stop cases are confirmed G12 attractors (`finish=stop, completion_tokens=0, content=null`)
consistent with `B10-S5b-integration.md` observation and `B7-G12-empty-stop-frequency.md`
50% rate measurement.

---

## Payload analysis

The failing request payload (B10 trace, 10 messages):

| Message | Role | Size |
|---------|------|------|
| [0] System prompt | system | 4192 chars (23 skills, all truncated ≤80 chars) |
| [1-3] User × 3 | user | 63 chars each |
| [4] assistant | tool_call: list_skills("") | — |
| [5] tool | list_skills result: [general(23)] | 64 chars |
| [6] assistant | tool_call: list_skills("general") | — |
| [7] tool | 23 skills list (truncated descs) | 4590 chars |
| [8] assistant | tool_call: describe_skill("eval_builder") | — |
| [9] tool | **full eval_builder dict** | **1381 chars** |

The `describe_skill("eval_builder")` response was **1381 chars** and included the full
`routing` field (intents, when_to_use × 3, when_not_to_use × 4, examples dict).

This is the **Pattern D** trigger: describe_skill response verbosity.

---

## Hypotheses tested

### Hypothesis A: MUST rule wording

**Test**: Remove the post-describe MUST rule from system prompt.

**Result**: 9/10 text stops (content present, ct > 0). The LLM replied in text instead
of calling tools. MUST rule removal makes behaviour **worse** (text replies instead of
tool invocations). Consistent with B7 H-a finding — MUST rules do not cause the attractor
but ARE needed to guide invocation.

**Verdict**: Hypothesis A rejected. MUST rule wording is not the cause.

### Hypothesis B: describe_skill response verbosity (routing field)

**Test (N=10 patch on B10 trace)**: Replace `messages[9].content` with routing-stripped
version (187 chars vs 1381 chars).

```
=== N-shot replay (n=10, --patch 'messages[9].content=<stripped>') ===
Finish reasons:
  tool_calls: 10
  stop: 0
```

**Result**: 0/10 empty-stop (0%) with routing stripped.

**Verdict**: Hypothesis B confirmed. The `routing` field in the describe_skill response
is the primary trigger for the Pattern D attractor.

### Hypothesis C: invoke_skill tool description truncation

**Test (N=10 patch on synthetic trace)**: Replace `tools[6].function.description` with
a 100-char version (vs 349 chars original).

```
=== N-shot replay (n=10, truncated invoke_skill desc) ===
Finish reasons:
  tool_calls: 9
  stop: 1
```

**Result**: 1/10 — within noise vs 2/10 baseline. No significant reduction.

**Verdict**: Hypothesis C rejected as primary fix. invoke_skill description truncation
does not meaningfully reduce attractor rate (consistent with G18 deferred status).

### Hypothesis D: message history shape

Not independently tested (explained by Hypothesis B — the 4590-char list response +
1381-char describe response together create the verbosity context that triggers the
P-b attractor).

---

## Root cause (Pattern D)

The G12 attractor in B10 is **Pattern D**: `describe_skill` response verbosity.

The B7 Option G fix addressed Patterns A and C (list_skills tool_response and system
prompt skill list truncation to ≤80 chars). Pattern D was not addressed: `describe_skill`
returned the **full** catalogue entry including the `routing` block.

The `routing` block for eval_builder contains:
- `intents`: ["task"]
- `when_to_use`: 3 items (Japanese + English text, ~200 chars)
- `when_not_to_use`: 4 items (~350 chars)
- `examples`: {positive: 4, negative: 3} (~200 chars)

Total routing block ≈ 780 chars, pushing the total describe response to 1381 chars in
B10 (23 skills + project-specific routing). This exceeds the P-b attractor threshold
(B7 cross-attractor analysis: last_tool_content_chars 38x higher in attractors vs baseline).

**Care boundary alignment**: The `routing` block is decision-guidance for the router's
SKILL SELECTION phase. Once the LLM has called `describe_skill`, it has already selected
the skill — the routing guidance is redundant. Stripping it from the describe response
is a structural pre-call environment fix (P3/P5 compliant, P7-clean).

---

## Fix chosen

**Approach**: Strip `routing` and `category` fields from `_describe_skill()` response.

- `routing`: primary verbosity trigger (780-1200 chars, DSL keywords)
- `category`: internal grouping metadata (redundant for invocation)
- Preserved: `name`, `description`, `input_artifact`, `input_fields` (all needed for invocation)

This reduces describe_skill response from ~1000-1400 chars to ~200 chars, well below
the P-b attractor threshold.

**P7 compliance**: `routing` and `category` are OS-level catalogue metadata field names
(present in every skill entry uniformly). No skill-specific strings, phase names, or
artifact type names used. Filtering applies identically to all skills.

---

## Evidence summary

| Scenario | Trace | N | G12 rate | Note |
|----------|-------|---|----------|------|
| Baseline (HEAD 4898ef9) | B10-S5b trace | 10 | 5/10 (50%) | Via llm_replay |
| Hypothesis B (routing strip, patched) | B10-S5b trace | 10 | 0/10 (0%) | Via llm_replay --patch |
| Hypothesis C (invoke_skill desc trunc) | Synthetic | 10 | 1/10 (10%) | Not significant |
| Hypothesis A (no MUST) | Synthetic | 10 | 0/10 G12, 9/10 text | Worse overall |

---

## References

- `B10-S5b-integration.md` — B10 dogfood observation (25% per session, 1 in 4 attempts)
- `B7-G12-context-root-cause.md` — H-b verification (218→80 chars = 100%→0%)
- `B7-G12-cross-attractor-pattern.md` — P-b (tool_response verbosity) as primary attractor mechanism
- `docs/en/decisions/0021-g12-attractor-structural-fix-design.md` — ADR-0021 (Option F/G)
- `giveup-tracker.md` G12 — full history
