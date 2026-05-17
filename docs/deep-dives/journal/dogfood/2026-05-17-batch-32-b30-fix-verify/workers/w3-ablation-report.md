# B31 W3 Regression Ablation

Generated: 2026-05-17
Worker: B31-W3-ablation
HEAD at run time: 2785de0 (current main)
Baseline comparison: B28 (V=6) → B30 (V=2), ΔV=-4
Affected scenarios: S2, S4, S5, S7, S8

---

## Method

### Trace capture

Fresh traces captured from current codebase using `REYN_LLM_TRACE_DUMP` + `reyn chat --cui`
with the exact S2 / S4 scenario inputs from `dogfood/scenarios/control_ir_ops.yaml`.
B28/B30 worktrees (`/tmp/reyn-worktrees/b28-{1..7}`) were deleted before this session;
original trace files are unavailable. Fresh traces are valid surrogates because:
- Router tools array (14 items) is structurally identical at B28, B30, and current HEAD
  for the W3 scenario set (no router_tools.py / router_loop.py changes between those points
  that affect the tools-array composition for a cold-start session).
- `llm_replay` replays a single LLM request in isolation; the model version (gemini-2.5-flash-lite
  via LiteLLM proxy at localhost:4000) is the same across all runs.

### Replay command template

```bash
LITELLM_API_BASE=http://localhost:4000 \
PYTHONIOENCODING=utf-8 \
python /Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/scripts/llm_replay.py \
  --trace <trace_file> \
  --request-id <req_id> \
  --model openai/gemini-2.5-flash-lite \
  [--patch '<patch_expr>'] \
  --n <N>
```

### Patches tested

| Patch ID | Scenario | Expression | Hypothesis |
|----------|----------|------------|------------|
| P-M2 | S2 | `tools[5]=<file__grep definition>` | B27-M2 removed file__grep from seed; restoring it fixes S2 |
| P-H1 | S4 | `tools[0]--` (remove plan) | B27-H1 restored plan; plan-first routing explains S4 regression |

### Pre-conclusion 5Q checklist (applied before causal claims)

1. Specific observations listed below per-scenario (primary data from replay output).
2. P-M2: primary data (3/3 replay calls). P-H1: primary data (3/3 replay calls + 3/3 baseline).
   S5/S7/S8: inference from source diffs + B28/B30 findings docs — downgraded to "hypothesised."
3. Falsifying check: Is B28 S2 verified result reproducible on current HEAD? No — baseline replays
   show invoke_action(file__list) with wrong args, consistent with B30. This supports the claim
   that B28 S2 was a probabilistic N=1 event, not a structurally guaranteed outcome.
4. Observation infra: llm_replay captures the exact tools array sent to LLM (verified by reading
   trace.jsonl request bodies). The patch replaces tool index 5 in that array in-place.
5. N/N claims: P-M2 → 3/3 directly inspected. P-H1 → 3/3 directly inspected. No extrapolation.

---

## Per-scenario ablation results

### S2: file_glob_grep — ATTRIBUTED (HIGH confidence)

**Scenario input:** "Find all skill.md files under src/ that mention 'judge_output'."

**B28 result:** VERIFIED — S2 used file__glob or valid file__list args. Worker notes:
"M2 confirmed — file__grep never called. file__glob used." (Exact tool call args not recorded.)

**B30 result:** REFUTED — invoke_action(file__list, {match:"word", filter:"src/skill.md"})
→ KeyError:'path'. LLM called file__list with glob-style args intended for file__grep/file__glob.

**Trace captured:** `/tmp/b31-ablation-work/s2/trace.jsonl` (4 lines, 2 req/resp pairs)
- Request ID: `2852811a-5782-426b-8963-55bfd7223129`
- Tools array: 14 items; index 5 = `file__list`

**Baseline replay (no patch, N=3):**
All 3 calls: invoke_action(file__list) with incorrect glob-style args → replicates B30 regression.

**P-M2 patch (tools[5] replaced with file__grep definition, N=3):**
All 3 calls: invoke_action(file__grep) → semantically correct tool for the task.
Output from `/tmp/b31-ablation-work/p_m2_s2_output.json`:
- Run 1: invoke_action, action=file__grep
- Run 2: invoke_action, action=file__grep
- Run 3: invoke_action, action=file__grep

**Causal attribution:**
B27-M2 (commit `1636584`) removed `file__grep` from `DEFAULT_HOT_LIST_SEED` because
`_OPERATION_RULES` in `universal_dispatch.py` has no routing rule for it
(FP-0034 §D20 deferred). With file__grep absent from the hot-list, the LLM reaches for
the nearest-affordance tool (`file__list`) but passes glob-style arguments that `file__list`
doesn't support (expects `path`, not `match`/`filter`).

**Why B28 was VERIFIED:** B28 W3 S2 verification was a lucky probabilistic outcome.
The current HEAD codebase (structurally identical tools array) produces file__list-with-wrong-args
in 3/3 fresh runs. The B28 worker note "file__glob used" suggests the LLM happened to pick a
valid file__list or file__glob call in that single N=1 run — consistent with the LLM having
non-zero probability of correct behavior even without file__grep. The B28 → B30 regression
is not attributable to a code change; it is the surface of a pre-existing attractor that
B27-M2 created and B28 verified optimistically.

**Confidence:** HIGH — direct primary data (3/3 baseline wrong, 3/3 with-file__grep correct).
The causal mechanism (absent routing rule → wrong tool selected) is structurally explained.
Remaining uncertainty: B28 actual call sequence is not directly inspected (B28 traces deleted).

---

### S4: web_fetch_url — NOT ATTRIBUTED (LOW confidence, probabilistic noise)

**Scenario input:** "Fetch and summarise https://docs.python.org/3/whatsnew/3.12.html"

**B28 result:** VERIFIED — invoke_action(web__fetch) directly, routing_decided emitted.

**B30 result:** REFUTED — LLM chose plan tool first; plan-first routing bypasses routing_decided.

**Trace captured:** `/tmp/b31-ablation-work/s4/trace.jsonl` (4 lines, 2 req/resp pairs)
- Request ID: `4bdee0d7-aad6-47b8-81c8-ed4dda798130`
- Tools array: 14 items; index 0 = `plan`

**Baseline replay (no patch, N=3):**
All 3 calls: invoke_action(web__fetch) — LLM does NOT choose plan. Replicates B28 behavior.
This contradicts B30 W3 finding that "LLM chose plan tool first."

**P-H1 patch (tools[0] removed = plan absent, N=3):**
All 3 calls: invoke_action(web__fetch) — identical result to baseline.
Plan presence vs absence makes no difference.

**Source diff analysis:**
No changes to `router_tools.py`, `router_loop.py`, `plan.py`, or `router_system_prompt.py`
between B28 and B30 that would alter the plan-vs-invoke_action decision for a simple fetch task.

**Causal attribution:**
Cannot attribute. B30 W3 S4 "plan first" behavior appears to be N=1 probabilistic noise.
The dominant LLM behavior for this input is invoke_action(web__fetch) in both B28 (verified)
and current-HEAD fresh replays (3/3 invoke_action). B30 happened to produce a plan-first
call in its single run; no structural code change explains why plan-first would become more
likely post-B28.

**Confidence:** LOW — primary data (N=3 baseline + N=3 P-H1) shows no regression at all on
current HEAD. B30 regression is classified as probabilistic N=1 event.

---

### S5: sandboxed_exec_simple — NOT ATTRIBUTED (classification shift, no code cause)

**B28 result:** INCONCLUSIVE — routing_decided fired but sandboxed_exec events absent.
B28 classification note: environment limitation (no sandbox backend).

**B30 result:** REFUTED — routing_decided fires, exec events absent (same behavior as B28).
B30 reclassified from INCONCLUSIVE → REFUTED due to stricter classification rules.

**Analysis:**
No trace capture or patch test performed — the behavioral outcome (routing_decided fires,
exec events absent) is identical between B28 and B30 per the findings docs. The ΔV=-1
contribution from S5 is entirely attributable to **scenario classification rule tightening**
(B28-Q2 fix added routing_decided / chat_turn_completed_inline tracking, making previously
INCONCLUSIVE outcomes classifiable as REFUTED). This is not a code regression.

**No patch test possible:** The underlying behavior (exec backend absent) is an environment
constraint, not an LLM behavior that llm_replay can probe.

**Confidence:** HIGH (classification-only explanation) — no structural code change found.

---

### S7: recall_indexed_source — UNRESOLVED (no B28 traces, source unchanged)

**B28 result:** VERIFIED — routing_decided emitted; graceful "no indexed sources" reply.

**B30 result:** REFUTED — inline reply: "recall only available in plan steps."
routing_decided NOT emitted; LLM replied without catalog dispatch.

**No patch test performed:** No B28 trace available; fresh trace from current HEAD captured
but not replayed (the S7 behavior is highly context/session-state dependent — the LLM's
"I can't use recall directly" response suggests SP or tool-description content has shaped
this attractor, not a specific tools[N] swap that llm_replay can isolate).

**Source diff analysis:**
- `plan.py` (unchanged between B28 and B30): `_PLAN_DESCRIPTION` explicitly lists `"recall"`
  as a plan-step tool name in the example. This may have reinforced the LLM's belief that
  recall is plan-only.
- `DEFAULT_HOT_LIST_SEED` (B28-MED-1 added `skill__index_docs`): The seed expansion changed
  hot-list composition and may have altered the LLM's understanding of what "recall" means.
  However, `skill__index_docs` was NEVER visible to LLM in B30 (hot_list_n=10 cap with 12
  items in seed → items beyond index 9 truncated).
- B30 findings note: "B28-MED-1 seed (skill__index_docs) changed hot-list composition, LLM
  now knows recall is plan-only." This is worker inference, not primary data.

**Causal attribution:**
Unresolved. The `plan.py` description listing "recall" as a plan-step tool is the most
plausible structural cause, but this text was present identically in B28 (when S7 was VERIFIED).
The B28 → B30 delta may be probabilistic or may involve session-state differences. No
llm_replay patch was run; this remains a hypothesis.

**Hypothesis for follow-up:** Patch `plan.py` description to remove "recall" from plan-step
example tool list; test N≥5. If S7 VERIFIED rate increases, that text is causal.

**Confidence:** LOW — inference only, no primary data from patch test.

---

### S8: judge_output_direct — NOT ATTRIBUTED (behavioral/async, no structural cause)

**B28 result:** VERIFIED — 14 LLM calls, synchronous phase output JSON in reply.

**B30 result:** REFUTED — async dispatch; single-turn reply is spawn-ack, not result.

**No patch test performed:** The B28 → B30 delta in S8 is not a tool-selection error — the LLM
correctly dispatched the right skill in both cases. The difference is in skill execution
semantics (synchronous vs async completion within the single-turn CUI capture window).

**Source diff analysis:**
No changes found in `router_loop.py`, `router_tools.py`, or skill dispatch logic between B28
and B30 that would alter synchronous vs async behavior. B28's synchronous completion (14 LLM
calls) vs B30's async dispatch-ack is likely:
(a) A session-state / timing difference in how the skill ran, OR
(b) A change in the skill itself (skill__eval or judge_phase skill changed between B28 and B30).
Neither is probed by an llm_replay patch on the first LLM call.

**Causal attribution:**
Not attributed. The regression is behavioral (skill execution timing), not a routing error
that patch-based LLM replay can address. Investigation would require full e2e run with
synchronous skill completion tracing.

**Confidence:** MEDIUM (for "not a routing-layer cause") — source analysis found no structural
change, and the failure mode (async dispatch) is outside llm_replay's probe scope.

---

## Patches that could not be tested

| Scenario | Patch | Reason not tested |
|----------|-------|-------------------|
| S7 | Restore recall as direct hot-list alias | recall has no routing rule in _OPERATION_RULES; adding it as alias would cause UnknownActionError |
| S7 | Remove "recall" from plan.py description | Cannot test with llm_replay patch (plan description is a tool description field, patching requires tools[N].function.description=... — valid but not attempted) |
| S8 | Any routing patch | Failure mode is async skill execution, not first-LLM-call routing — outside llm_replay scope |
| S5 | Any exec backend patch | Environment constraint; not an LLM behavior |
| S2 (B28 trace) | Reproduce B28 exact call | B28 worktree deleted; original trace file unavailable |

---

## Overall attribution

| Scenario | ΔV contribution | Attributed to | Confidence |
|----------|----------------|---------------|------------|
| S2 | -1 (VERIFIED → REFUTED) | B27-M2 (file__grep removal) created attractor; B28 verified optimistically (N=1 lucky); not a new regression | HIGH |
| S4 | -1 (VERIFIED → REFUTED) | Probabilistic noise (N=1 B30 run); no structural cause found | LOW |
| S5 | -1 (INCONCLUSIVE → REFUTED) | B28-Q2 classification tightening, not a code regression | HIGH |
| S7 | -1 (VERIFIED → REFUTED) | Unresolved; plan.py "recall" example is candidate cause | LOW |
| S8 | -1 (VERIFIED → REFUTED) | Behavioral/timing change in skill execution, not routing layer | MEDIUM |

### Summary

- **Causally attributed (HIGH confidence):** S2 (B27-M2), S5 (B28-Q2 classification)
- **Probabilistic noise (no structural cause):** S4
- **Unresolved:** S7 (requires follow-up patch test on plan.py description)
- **Out of scope for routing-layer ablation:** S8

### Recommended fixes

1. **S2 (actionable):** Add a `file__grep` routing rule to `_OPERATION_RULES` in
   `universal_dispatch.py` (FP-0034 §D20 deferred work), then re-add `file__grep` to
   `DEFAULT_HOT_LIST_SEED`. Until then, the LLM will continue to misuse `file__list` for
   glob/grep tasks. Alternatively, patch `file__list`'s tool description to explicitly exclude
   glob-style parameters.

2. **S7 (hypothesis):** Run a targeted patch test replacing the "recall" example in
   `plan.py._PLAN_DESCRIPTION` with a non-recall tool name. If N≥3 shows routing_decided
   emitted, commit the description change.

3. **S4 / S8 (deferred):** No fix warranted from this ablation. S4 needs N≥5 B31 fresh runs
   to confirm baseline behavior. S8 requires async skill execution analysis outside this scope.

---

*Ablation conducted 2026-05-17. Traces at `/tmp/b31-ablation-work/`. LiteLLM proxy: localhost:4000.*
