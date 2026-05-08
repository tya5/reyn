# B5R2-A: Curry Recipe — Scenario A Retest 2

## Verdict: partial (B5-H1 partial improvement confirmed; invoke_skill still not reached)

## Setup
- Agent topology: `default` + `specialist` (auto-edge `default ↔ specialist`)
- Input: `specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて`
- HEAD: `ca116f3` (B5-H1 fix)
- Runs: 1 (clean `.reyn/` before run)

## dogfood_trace summary

```
============================================================
DOGFOOD TRACE SUMMARY
============================================================

[Skill Chain]  (0 workflow(s))

[Tool Calls]  (4 important tool call(s))
  [ 1] delegate_to_agent({"request": "カレーの簡単な作り方を教えて", "to": "specialist"})  caller=default
  [ 2] list_skills({"path": ""})  caller=specialist
  [ 3] list_skills({"path": "general"})  caller=specialist
  [ 4] describe_skill({"name": "direct_llm"})  caller=specialist

[Peer Failures / Chain Discards]  (1 event(s))
  peer_reply_failed_surfaced: peer=specialist  reason=router completed without producing a text reply

[Interventions]  dispatch=0  resolve=0
[Agent Messages]  (2 message(s))
  ?: 
  ?: 

=== Cost Summary ===
  Total: $0.000897  |  8,748 tokens  |  5 calls
  Per-model:
    gemini-2.5-flash-lite: $0.000897  8,748 tokens  (5 calls)
```

## dogfood_trace chain

```
=== Skill / Tool Chain ===
[T+2.0s] tool: delegate_to_agent({"request": "カレーの簡単な作り方を教えて", "to": "specialist"})
[T+3.0s] tool: list_skills({"path": ""})
[T+4.0s] tool: list_skills({"path": "general"})
[T+4.0s] tool: describe_skill({"name": "direct_llm"})
```

## Observed event sequence

1. `default` router → `delegate_to_agent(specialist)` ✅
2. `specialist` router → `list_skills(path="")` → `[{category: general, count: 10}]` ✅
3. `specialist` router → `list_skills(path="general")` → direct_llm, eval, skill_improver listed ✅
4. `specialist` router → `describe_skill(direct_llm)` ← **NEW vs B5-FV** ✅
5. `specialist` router → returns `agent_response` with empty text (no `invoke_skill`) ✗
6. `default` → `peer_reply_failed_surfaced` ✗
7. Output: `Could not get a result from agent 'specialist'` ✗

## B5-H1 fix effect assessment

**Improvement confirmed**: In B5-FV, specialist stopped after 2 `list_skills` calls.
After B5-H1 fix, specialist now progresses one step further: `list_skills → list_skills → describe_skill`.
The restored individual bullet structure did strengthen the discovery-path signal enough
to trigger `describe_skill`. However, the model (gemini-2.5-flash-lite) still exits after
`describe_skill` without calling `invoke_skill`.

**Root cause (persistent)**: Bullet 4 ("After describe_skill, you MUST call invoke_skill
or explain in text why not") is being ignored by the LLM. The MUST appears in a separate
bullet (per B5-H1 fix), yet gemini-2.5-flash-lite returns silently.

## B4-H1 fix effect (narrator reply path)

Not exercised — specialist never reached `invoke_skill`, so no skill was run and no
narrator reply was produced. The B4-H1 fix cannot be confirmed or denied in this scenario.

## Expected checklist

| Check | Result |
|-------|--------|
| specialist list_skills 到達 | ✅ (both levels) |
| specialist describe_skill 到達 (new) | ✅ (B5-H1 improvement) |
| specialist invoke_skill 到達 | ✗ (still stops at describe_skill) |
| skill output → narrator reply → agent_replies → default | ✗ (never triggered) |
| default が user にカレーレシピを提示 | ✗ |
| peer_reply_failed_surfaced が出ない | ✗ (still emitted) |

## New finding: B5R2-H1 (HIGH)

`describe_skill` → stop pattern: even with individual MUST bullet for post-describe_skill
commit obligation, gemini-2.5-flash-lite silently returns after describe_skill without
invoking the skill. The fix partially worked (added one step) but is insufficient.

Possible follow-up fixes:
- Add a max_iterations=1 on describe_skill followed by mandatory invoke_skill call
- Inject a router-level check: if describe_skill was last tool and no invoke_skill, inject
  a synthetic "you must now call invoke_skill" user turn
- Fallback: auto-invoke when describe_skill result is present and no invoke_skill followed
