# B7-G12: empty-stop context root cause investigation via `--patch`

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 310e00f |
| Trace file | `.reyn/llm_trace_g12_run1.jsonl` (fresh dogfood, attractor captured same session) |
| Attractor request_id | `e7319d6f-1fce-485d-ab11-0751e9aa9794` |
| Baseline rate | **10/10 (100%)** — see note below |
| Total replays | 70 (baseline 10 + 4 × 10 hypothesis + 3 × 5 breakdown) |

---

## Note: Baseline divergence from B7-G12 (50% → 100%)

The previous B7-G12-empty-stop-frequency measurement (`883da2c8-...`) observed a 50%
rate on a trace captured in worktree `agent-adbe9bd352bc7b644` (since removed).
This session's baseline uses a freshly captured trace from the current worktree.
The new trace shows 10/10 (100%) empty-stop, indicating this payload is a stronger
attractor than the previous one. The structural payload is identical (`msgs=8, tools=11,
tokens_in=2271`), but model response distribution shifted. This is consistent with the
probabilistic nature already established — different sessions can land on different stable
modes. Context experiments are valid relative to this session's baseline.

---

## Setup

### Payload structure (attractor context)

```
msg[0] role=system  : router behaviour prompt (MUST rules present)
msg[1] role=user    : "direct_llm skill を使って、カレーのレシピを教えてもらって"
msg[2] role=user    : (duplicate — same text)
msg[3] role=user    : (duplicate — same text)
msg[4] role=asst    : tool_call list_skills("")
msg[5] role=tool    : {"status":"ok","data":[{"category":"general","count":10}]}
msg[6] role=asst    : tool_call list_skills("general")
msg[7] role=tool    : list_skills("general") result — 10 skills with descriptions (1342 chars)
tools               : 11 tools (list_skills, describe_skill, invoke_skill, + 8 others)
tokens_in           : 2271
```

Note: `msg[1]–msg[3]` are three identical user messages. This is a suspected router-loop
artefact (each retry of the user turn injects a new user message without deduplication).
H-d tested whether deduplication affects the empty-stop rate.

### Experiment design

Four hypotheses tested via direct payload mutation (equivalent to `--patch` approach,
executed via Python driver `scripts_g12_exp.py`):

- **H-a**: Remove MUST rules from system prompt (`msg[0]`)
- **H-b**: Shorten `list_skills("general")` tool response (`msg[7]`)
- **H-c**: Reduce tool catalog from 11 to 3 (list_skills, describe_skill, invoke_skill only)
- **H-d**: Remove duplicate user messages (`msg[2]`, `msg[3]` — keep only `msg[1]`)

---

## Hypotheses

| ID | Hypothesis | Mechanism |
|----|-----------|-----------|
| H-a | system prompt MUST rules trigger provider-level "over-restrictive" detection → `completion_tokens=0` | Gemini sees "you MUST" as conflicting signal after it already satisfied prior steps |
| H-b | verbose `list_skills` response signals "task done" to the model | Long, complete skill catalogue reduces model's sense of urgency to act further |
| H-c | large tool catalog (11 tools) dilutes attention away from the 3 relevant tools | Model cannot decide which tool to call when given too many options |
| H-d | 3 duplicate user messages reinforce "already handled" signal | Repeated identical messages look like echo/completion to the model |

---

## Experiments

### Baseline (N=10)

| Run | finish_reason | tool_calls | tokens_out | Result |
|-----|--------------|------------|-----------|--------|
| 1–10 | stop | (none) | 0 | **empty-stop** |

**Rate: 10/10 (100%)**

### H-a: Remove MUST rules (N=10)

Patch: removed both MUST-rule sentences from `msg[0].content`.
- "After list_skills reveals at least one matching skill, you MUST call describe_skill or invoke_skill. Do NOT reply directly."
- "After describe_skill, you MUST call invoke_skill or explain in text why not; never stop silently after investigation."
- System prompt length: 3162 → 2906 chars. `"MUST" not in new_prompt` confirmed.

| Run | Result |
|-----|--------|
| 1–10 | **empty-stop** |

**Rate: 10/10 (100%) — no change from baseline**

### H-b: Shorten list_skills tool response (N=10)

Patch: replaced `msg[7].content` with name-only list (no descriptions).
- Original: 1342 chars (10 skills with multi-sentence descriptions)
- Patched: 285 chars (10 skill names, no descriptions)
- Removed: 1057 chars of description text

| Run | finish_reason | tool_calls | tokens_out | Result |
|-----|--------------|------------|-----------|--------|
| 1–10 | tool_calls | describe_skill("direct_llm") | 18 | **rescued** |

**Rate: 0/10 (0%) — complete elimination**

### H-c: Reduce tool catalog (N=10)

Patch: removed 8 of 11 tools, kept only list_skills, describe_skill, invoke_skill.

| Run | Result |
|-----|--------|
| 1–10 | **empty-stop** |

**Rate: 10/10 (100%) — no change from baseline**

### H-d: Remove duplicate user messages (N=10)

Patch: removed `msg[2]` and `msg[3]` (duplicate user messages), keeping single user message.
Message count: 8 → 6.

| Run | Result |
|-----|--------|
| 1–10 | **empty-stop** |

**Rate: 10/10 (100%) — no change from baseline**

---

## H-b breakdown experiments (N=5 each)

H-b eliminated empty-stop. Three sub-experiments identified the specific contributor.

### H-b1: Shorten only skill_improver description

`skill_improver` has by far the longest description (218 chars vs 48–91 chars for others):
> "Iteratively improve an existing skill by working on a temp copy, running eval, planning DSL changes, applying them, and re-evaluating until a score threshold is met. Only copies changes back to the original on success."

Patched `skill_improver.description` to "Iteratively improve an existing skill" (38 chars).

**Rate: 0/5 (0%) — complete elimination from single-skill change**

### H-b2: Remove all descriptions

Removed all `description` fields from the 10 skill entries.

**Rate: 0/5 (0%)**

### H-b3: Keep only direct_llm description

Kept `direct_llm.description`, removed all others.

**Rate: 0/5 (0%)**

---

## Summary table

| Experiment | Empty-stop rate | Delta vs baseline | Δ tokens context (approx) |
|-----------|----------------|-------------------|--------------------------|
| Baseline | **10/10 (100%)** | — | — |
| H-a: Remove MUST rules | 10/10 (100%) | 0 | −256 chars |
| H-b: Shorten tool response | **0/10 (0%)** | **−100pp** | −1057 chars |
| H-c: Reduce tool catalog | 10/10 (100%) | 0 | −8 tools |
| H-d: Remove duplicate user msgs | 10/10 (100%) | 0 | −2 messages |
| H-b1: Shorten skill_improver only | **0/5 (0%)** | **−100pp** | −180 chars |
| H-b2: Remove all descriptions | **0/5 (0%)** | **−100pp** | −867 chars |
| H-b3: Keep only direct_llm desc | **0/5 (0%)** | **−100pp** | −649 chars |

---

## Findings

### Primary finding: H-b — tool response verbosity is the decisive factor

**The `list_skills("general")` tool response description text is the primary trigger
of the empty-stop attractor in this payload family.**

- H-a (MUST rules), H-c (tool catalog size), H-d (duplicate messages) all had zero
  effect. The model's empty-stop behaviour is insensitive to these context elements
  under the tested payload.
- H-b produced 100% rescue — eliminating skill description text from the tool response
  completely eliminates the attractor.
- H-b1 shows even a single-description shortening (skill_improver alone, −180 chars)
  is sufficient for complete rescue. This suggests the trigger is not a simple token
  budget issue but may relate to specific content or cumulative description verbosity.

### Secondary finding: H-a null result reframes RETRO-H4

RETRO-H4 found that MUST rules were present in the payload at attractor time and
concluded "the LLM saw the rule and did not honour it." H-a confirms this: removing
the MUST rules has no rescue effect. This means:

1. MUST rules are neither causing nor preventing the empty-stop — they are irrelevant
   to the attractor mechanism.
2. The accumulated MUST rules (added across B2-H1 → B6-S2) were never addressing the
   true root cause. They were a prompt accumulation anti-pattern (as documented in
   `feedback_prompt_design.md`).
3. Future prompt changes targeting the attractor via MUST rules are unlikely to have
   effect unless they also address the tool response verbosity.

### Tertiary finding: H-b1 — skill_improver is the critical description

`skill_improver`'s description (218 chars) is 2.9× longer than the 10-skill average
(~75 chars). Shortening this single description is sufficient for complete rescue.
This raises two possible mechanisms:

1. **Length threshold**: the description crosses a context "tipping point" at which
   the model treats the context as task-saturated. The exact threshold is not
   quantified.
2. **Semantic content**: the description mentions `eval`, `planning`, `score threshold`
   — concepts that may signal "evaluation complete" to the model. Content experiments
   were not conducted; this is a remaining question.

### Causal ranking

1. **`list_skills` tool response description text** — causal (H-b confirmed)
   - Specifically `skill_improver` description length/content (H-b1)
2. **MUST rules** — no causal role (H-a null)
3. **Tool catalog size** — no causal role (H-c null)
4. **Duplicate user messages** — no causal role (H-d null)

---

## Implications

### Care boundary

The finding establishes a clear OS-side care boundary:

| Element | Can context fix rescue it? | Care owner |
|---------|---------------------------|------------|
| list_skills response verbosity | Yes (H-b: 100% rescue) | Skill / router |
| MUST rules | No (H-a: 0% change) | LLM (model limit) |
| Tool catalog size | No (H-c: 0% change) | LLM (model limit) |
| Duplicate messages | No (H-d: 0% change) | Router (separate issue) |

The attractor is primarily a **data quality problem in the tool response**, not a prompt
design problem. The fix is structural: truncate skill descriptions in `list_skills` output
to a consistent max length (e.g. 80 chars).

### ADR 0021 Option F validity

ADR 0021 adopted Option F (detect + explicit failure UX, no auto-rescue) on the rationale
that "context に問題がないのに空文字だった場合のケース、これは llm の問題であって、
reyn で過剰ケアすべきではない."

This experiment partially revises that framing:

**The context DOES have a problem** — the `list_skills` tool response includes descriptions
that are longer than the model can reliably process as "incomplete context." The attractor
is not a pure LLM model-limit phenomenon; it is model-limit applied to over-verbose tool
output.

However, Option F's core validity is maintained:

1. Even if context fix is possible (H-b), context fixes reduce attractor rate but cannot
   guarantee zero-rate at all payload sizes. H-b1 suggests 180-char description shortening
   is sufficient for this specific payload; it may not be for all payloads.
2. The correct fix is upstream (skill/router truncation), not OS-layer retry. Option F's
   principle — Reyn detects and surfaces, does not absorb — remains correct; the surface
   event now points users to the tool-response verbosity cause rather than a model glitch.
3. The duplicate user messages (H-d null) remain an unremediated structural issue, but
   it did not contribute to the attractor in this test payload.

**Recommendation**: Pair Option F with a `list_skills` description truncation (≤80 chars
per skill). This makes Option F's explicit failure UX far less frequent without introducing
OS-layer retry.

---

## Out of scope

- Full N-shot sweep across all possible description lengths (binary search for exact threshold)
- Cross-payload validation (different attractor scenarios, different skill catalogues)
- Semantic vs length isolation for skill_improver content
- ADR 0021 update (separate agent, collision avoidance)

---

## Next action

1. **Fix `list_skills` response verbosity**: truncate skill descriptions to ≤80 chars
   in the router's skill list formatting (not in the skill metadata itself). Implement
   as a pre-formatting step in the router context build, not in the OS (P7 clean).
2. **Verify fix**: replay the attractor payload after truncation fix to confirm <10%
   baseline rate.
3. **Remove accumulated MUST rules**: H-a confirms they are ineffective; they are
   prompt bloat. Remove them per `feedback_prompt_design.md` (MUST rule accumulation
   anti-pattern).
4. **Investigate duplicate user messages** (H-d showed no attractor effect, but the
   3× duplication is a separate correctness issue in the router loop).

---

## LLM cost

| Item | Calls | Approx tokens/call | Estimated cost |
|------|-------|-------------------|----------------|
| Fresh dogfood (run1) | 3 router calls | ~2100 avg | ~$0.0006 |
| Baseline N=10 | 10 | 2271 in | ~$0.0022 |
| H-a N=10 | 10 | ~2015 in (shorter sys) | ~$0.0020 |
| H-b N=10 | 10 | ~1514 in (shorter tool resp) | ~$0.0015 |
| H-c N=10 | 10 | ~2150 in (fewer tools) | ~$0.0021 |
| H-d N=10 | 10 | ~1950 in (fewer msgs) | ~$0.0019 |
| H-b breakdown 3×5 | 15 | ~1600 avg | ~$0.0023 |
| **Total** | **~78 calls** | | **~$0.013** |

## References

- `scripts/llm_replay.py` — payload replay infrastructure
- `scripts/detect_attractor.py` — attractor detection heuristic
- `docs/en/decisions/0021-g12-attractor-structural-fix-design.md` — ADR 0021 Option F
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-empty-stop-frequency.md`
  — previous frequency measurement (50% baseline, different session)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H4-attractor-prompt-evidence.md`
  — MUST rule injection evidence (RETRO-H4)
- `feedback_prompt_design.md` — MUST rule accumulation anti-pattern
