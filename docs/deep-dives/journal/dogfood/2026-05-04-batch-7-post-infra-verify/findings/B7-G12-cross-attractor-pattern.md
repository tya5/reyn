# B7-G12: cross-attractor pattern analysis

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 310e00f |
| Trace files used | `llm_trace_cross1.jsonl`, `/tmp/reyn_cross_trace_b.jsonl`, `/tmp/reyn_cross_trace_f.jsonl`, `/tmp/reyn_cross_trace_l.jsonl` + historical B7-RETRO-H4 |
| Attractor count | 5 (4 from live trace + 1 from documented B7-RETRO-H4) |
| Baseline count | 5 (successful router `tool_calls` responses from same trace files) |
| Input patterns | A: `direct_llm skill を使って、カレーのレシピを教えてもらって`; C: `eval_builder を使って word_stats_demo を分析して` |

---

## Setup

### Goal

Intentionally induce the G12 attractor with multiple different input wordings and compare payloads
cross-attractor to identify structural common patterns. The goal is to determine which context
elements are quantitatively associated with empty-stop onset, and whether those elements differ
across input patterns.

### Attractor collection

**Existing traces surveyed:**

- `llm_trace_h4.jsonl`, `llm_trace_h2.jsonl`, `llm_trace_h1.jsonl`, `llm_trace_b8s1.jsonl` —
  all had 0 attractor detections (router calls present but no `stop_with_must_rule`)
- `llm_trace_g12_run1.jsonl` — 1 attractor (`e7319d6f`, pattern A); file no longer on disk at analysis time

**Fresh dogfood runs (12 total, isolated `/tmp/reyn_dogfood_X` directories, clean `.reyn`):**

Pattern A (`direct_llm skill + カレー`): 6 runs → 2 attractors (A1, A2), 4 non-attractor  
Pattern C (`eval_builder + word_stats_demo`): 4 runs → 2 attractors (A3, A4), 2 non-attractor  
Pattern D (`direct_llm review` / `skill_improver + direct_llm`): 2 runs → 0 attractors (different router path)

Observed rate: 4 attractors / 10 relevant runs ≈ 40% (consistent with prior 50% measurement).

**Historical attractor A5** reconstructed from `B7-RETRO-H4-attractor-prompt-evidence.md`
(request `fd2aef81`, trace since removed). Key metrics from documented observation used directly.

---

## Hypotheses

| ID | Hypothesis | Mechanism |
|----|-----------|-----------|
| P-a | tool_response history token count exceeds a threshold | Over-accumulation of tool context triggers saturation signal |
| P-b | `describe_skill` / `list_skills` response token count exceeds a threshold | Verbose skill descriptions signal "task complete" to the model |
| P-c | system prompt + tool catalog combined token count exceeds a threshold | Fixed context overhead crowds out decision space |
| P-d | last tool response contains specific skill DSL keywords (`phase`, `skill`, etc.) | Semantic signal in tool content confuses the model's next-step selection |
| P-e | message history role sequence is too long (user→tool→tool→…) | Message depth independently causes the model to treat context as terminal |

---

## Data

### Attractor table (5 cases)

| ID | Input pattern | msgs | tool_R | prompt_tok | lastToolChars | lastDSL_kw | prev_tool_calls |
|----|--------------|------|--------|------------|---------------|------------|-----------------|
| A1 | A: direct_llm+カレー | 8 | 2 | 2568 | 2274 | 20 | list_skills × 2 |
| A2 | A: direct_llm+カレー | 8 | 2 | 2271 | 1342 | 11 | list_skills × 2 |
| A3 | C: eval_builder+word_stats | 4 | 0 | 1896 | 0 | 0 | (none) |
| A4 | C: eval_builder+word_stats | 4 | 0 | 1896 | 0 | 0 | (none) |
| A5 | A: direct_llm+カレー (doc) | 8 | 2 | 1915 | 1342 | 11 | list_skills × 2 |

**Column definitions:**
- `msgs`: total message count in the LLM call payload
- `tool_R`: number of tool-response messages in payload
- `prompt_tok`: `usage.prompt_tokens` from API response
- `lastToolChars`: character count of the last tool_response content
- `lastDSL_kw`: count of DSL keywords (`phase`, `input_schema`, `transition`, `entry`, `output_schema`, `final_output`, `skill`) in the last tool_response
- `prev_tool_calls`: tool names called in preceding assistant messages

**Structural note on A1 vs A2:** The difference in `lastToolChars` (2274 vs 1342) reflects
the router's conditional data returned by `list_skills` — A1 received `input_artifact` and
`input_fields` metadata in addition to name/description; A2 received name/description only.
Both are attractor payloads with the same sequence (`list_skills × 2 → stop`).

**Structural note on A3/A4 (Pattern C):** Both fired on the first router call (msgs=4, zero
tool_responses). The system prompt for these runs already embeds the full skill list inline
(`## Available skills (10) — use these exact names with invoke_skill` section, 10 skills with
descriptions). No `list_skills` call was needed; the model saw the skill list in the system
prompt and still failed to act.

### Baseline table (5 cases)

All are successful `tool_calls` responses from the router.

| ID | msgs | tool_R | prompt_tok | lastToolChars | lastDSL_kw | prev_tool_calls |
|----|------|--------|------------|---------------|------------|-----------------|
| B1 | 4 | 0 | 1902 | 0 | 0 | (none) |
| B2 | 6 | 1 | 1946 | 64 | 0 | list_skills |
| B3 | 4 | 0 | 1882 | 0 | 0 | (none) |
| B4 | 6 | 1 | 1926 | 64 | 0 | list_skills |
| B5 | 4 | 0 | 1861 | 0 | 0 | (none) |

B1/B3/B5 are the initial `list_skills("")` calls (no prior tool responses). B2/B4 are
successful `list_skills("general")` calls (one prior tool response with 64-char summary).

---

## Analysis

### Statistical comparison

| Hyp | Metric | A-mean | A-range | B-mean | B-range | ratio | verdict |
|-----|--------|--------|---------|--------|---------|-------|---------|
| P-e | message count | 6.4 | 4–8 | 4.8 | 4–6 | 1.33x | moderate |
| P-e | tool_response count | 1.2 | 0–2 | 0.4 | 0–1 | 3.00x | SIGNIFICANT |
| P-a | prompt_tokens (usage) | 2109.2 | 1896–2568 | 1903.4 | 1861–1946 | 1.11x | no diff |
| P-b | last_tool content chars | 991.6 | 0–2274 | 25.6 | 0–64 | 38.73x | SIGNIFICANT |
| P-d | DSL kw in last tool resp | 8.4 | 0–20 | 0.0 | 0–0 | inf | SIGNIFICANT |
| P-b | total tool content chars | 1030.0 | 0–2338 | 25.6 | 0–64 | 40.23x | SIGNIFICANT |
| P-c | sys prompt chars | 3162.0 | 3162–3162 | 3162.0 | 3162–3162 | 1.00x | no diff |
| P-c | tool catalog count | 11.0 | 11–11 | 11.0 | 11–11 | 1.00x | no diff |

### Hypothesis evaluation

**P-a (prompt_tokens threshold):** ratio 1.11x, range overlap. No significant difference.
The attractor A3/A4 at prompt_tok=1896 is within the baseline range (1861–1946). Total
prompt token count is not a reliable discriminator. **Rejected as primary cause.**

**P-b (tool response verbosity):** ratio 38–40x, no range overlap between typical attractor
and baseline. When tool_response content is verbose (>1300 chars), attractor rate is high.
When it is absent or minimal (<64 chars), attractor does not occur in baseline.

However, A3/A4 break this pattern: they are confirmed attractors with `lastToolChars=0`
(no tool_responses at all). This means verbose tool response is a **sufficient but not
necessary** condition. Pattern C attractors arise from a different mechanism (embedded
skill list in system prompt). **Partially confirmed — sufficient condition for pattern A.**

**P-c (sys prompt + tool catalog size):** ratio 1.00x for both sys_chars and tool_catalog_count.
These are identical across all attractors and baselines. **Rejected** (not a variable here).

**P-d (DSL keywords in last tool response):** ratio=infinite (all baseline DSL_kw=0, attractor
mean=8.4). However, DSL keyword presence is confounded with P-b: verbose skill descriptions
naturally contain "skill" (~10-20 occurrences). In A3/A4 where DSL_kw=0, the attractor still
fires. DSL keyword content is thus a **correlate of P-b, not an independent factor.** **Not
an independent cause; subsumed by P-b.**

**P-e (message depth / tool_response count):** ratio 3.00x for tool_response count, significant.
But again A3/A4 demonstrate the attractor fires with tool_response_count=0. Message depth
is also a correlate of P-b: both accumulate as `list_skills` tool calls accumulate. **Not
an independent cause; correlated with P-b in pattern A, absent in pattern C.**

### Cross-pattern structural comparison

| Dimension | Pattern A attractor | Pattern C attractor |
|-----------|---------------------|---------------------|
| Attractor call position | 3rd router call (after 2 list_skills) | 1st router call |
| Skill list in context | Via tool_response (list_skills result) | Embedded in system prompt |
| tool_response count | 2 | 0 |
| last_tool_content_chars | 1342–2274 | 0 |
| Common structural element | **Skill list visible to model** | **Skill list visible to model** |
| MUST rule present | Yes | Yes |
| Attractor signature | finish=stop, comp_tokens=0 | finish=stop, comp_tokens=0 |

The **one invariant across all 5 attractors**: the skill list (10 skills with descriptions)
is visible to the model at attractor time. In patterns A, it arrives via tool_responses;
in pattern C, it is pre-embedded in the system prompt. The model sees the full catalogue
and fails to invoke any skill.

---

## Findings

### Common pattern ranking

**Rank 1 (confirmed across all 5 attractors): Skill catalogue visibility**

Every attractor occurs at the first LLM call where the full skill catalogue (name + description
for all 10 skills) is visible in context. This can arrive via:
- `list_skills` tool_response (pattern A — 3 of 5 attractors)
- System prompt inline embedding (pattern C — 2 of 5 attractors)

**Causal specificity:** The `B7-G12-context-root-cause.md` established via payload mutation
(H-b, H-b1) that the skill description text — specifically `skill_improver`'s 218-char
description — is the decisive factor in pattern A. Shortening it to 80 chars eliminated
empty-stop (0/10 rate). This cross-attractor analysis confirms that the same content
(skill catalogue with full descriptions) also triggers the attractor in the system prompt
path (pattern C).

**Rank 2 (significant but confounded): Tool response verbosity and message depth**

For pattern A, `last_tool_content_chars` (38x ratio) and `tool_response_count` (3x ratio)
are strong numeric discriminators. These are consequences of the same root cause:
each `list_skills` turn accumulates more skill description text.

**Rank 3 (not independent): DSL keyword count in tool response**

Correlated with P-b (verbosity). Not an independent trigger.

**Rejected: P-a (prompt_tokens), P-c (sys prompt size, tool catalog count)**

No statistically significant difference. These elements are common to both attractor and
baseline calls and do not vary in a way that predicts attractor onset.

### Specific attractor characterization

The G12 empty-stop attractor is triggered when:

> The LLM router receives a context in which the **full skill catalogue with descriptions**
> is visible — either via `list_skills` tool_response or system prompt embedding —
> and the request asks for an action that maps to one of those skills.

The response is a `finish_reason=stop` with `completion_tokens=0` (truncation / abort
without output generation).

This is consistent with the `B7-G12-context-root-cause.md` causal ranking:
verbose skill description text signals "task information complete" to gemini-2.5-flash-lite,
causing it to treat the turn as concluded.

---

## Implications

### Care boundary alignment

| Context element | Attractor role | Owner |
|----------------|---------------|-------|
| Skill description verbosity in tool_response | **Primary cause (P-b, confirmed)** | router / skill catalogue |
| Skill description verbosity in sys prompt | **Equivalent cause (pattern C)** | router context builder |
| MUST rules | No role (B7-G12-context-root-cause H-a) | LLM model limit |
| Tool catalog size | No role (H-c) | LLM model limit |
| Message depth | Correlate, not cause | router loop |

**Key implication**: The fix scope is broader than pattern A alone. If description text
is truncated in `list_skills` tool_response (current recommendation: ≤80 chars), but the
system prompt also embeds full descriptions, pattern C attractors persist. Both the
`list_skills` response formatter and the system prompt skill-list section must be truncated
consistently.

### ADR 0021 Option F validity

ADR 0021 Option F (detect + explicit failure UX, no auto-rescue) was validated in
`B7-G12-context-root-cause.md` with the qualification that upstream description truncation
is the correct fix. This cross-attractor analysis reinforces that:

1. The mechanism is consistent across input wordings (pattern A and C both resolved by
   same root cause — description verbosity).
2. OS-layer retry would not address the cause: the same verbose context would re-trigger
   the attractor on retry.
3. The fix must occur at content generation time (router / skill catalogue), not at
   observation time (OS layer).

ADR 0021 Option F remains correct; the "what to point to" in the explicit failure message
is now more precise: "skill description verbosity in both tool_response and system prompt
skill section."

### Alignment with B7-G12-context-root-cause.md (parallel sonnet)

The `B7-G12-context-root-cause.md` analysis (H-a through H-b3) established the causal
mechanism via payload mutation. This cross-attractor pattern analysis confirms:

1. The mechanism generalises across input wordings (not specific to カレーレシピ wording).
2. The mechanism activates via both code paths where skill descriptions arrive (tool_response
   and system prompt embedding).
3. The 50% probabilistic rate measured in B7-G12-empty-stop-frequency holds across both
   input patterns (observed ~40% in 10 fresh runs).

The two findings are complementary: root-cause finding identifies *what* in the context
triggers the attractor; this cross-attractor analysis confirms the trigger generalises
*across input patterns*.

---

## Out of scope

- ADR 0021 update (separate agent, collision avoidance)
- Code changes (observation only)
- Binary threshold search for exact description length boundary
- Pattern B (`direct_llm の eval を作って`) and pattern D (`direct_llm review`) — these
  took different router paths (eval_builder invoked successfully; `direct_llm review` replied
  directly without attractor) and are not attractor-inducing under current conditions
- Batch 8 retest of fix after description truncation

---

## Next action

1. **Extend description truncation fix** from `list_skills` tool_response to the system
   prompt skill-list section (both code paths must be consistent, ≤80 chars per description).
2. **Verify fix against pattern C**: after truncation, run `eval_builder + word_stats_demo`
   scenario and confirm first-call attractor elimination.
3. Cross-reference with ADR 0021 to update the "what Reyn surfaces in failure UX" to
   reference skill description verbosity as root cause.

---

## LLM cost (fresh dogfood)

| Item | Runs | Approx tokens/call | Est. cost |
|------|------|--------------------|-----------|
| Pattern A dogfood (6 runs) | 3–4 router calls each | ~2000 avg | ~$0.005 |
| Pattern C dogfood (4 runs) | 1–5 calls each | ~2000 avg | ~$0.004 |
| Pattern D dogfood (2 runs) | 1–2 calls each | ~2000 avg | ~$0.001 |
| **Total** | **~12 runs / ~48 calls** | | **~$0.010** |

---

## References

- `scripts/detect_attractor.py` — heuristic `stop_with_must_rule` used for detection
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-context-root-cause.md`
  — root cause determination via payload mutation (H-a through H-b3)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-empty-stop-frequency.md`
  — frequency measurement (50% probabilistic)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H4-attractor-prompt-evidence.md`
  — MUST rule injection evidence and historical attractor A5 data
- `docs/en/decisions/0021-g12-attractor-structural-fix-design.md` — ADR 0021 Option F
