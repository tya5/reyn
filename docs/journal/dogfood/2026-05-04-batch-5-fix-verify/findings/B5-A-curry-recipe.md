# B5-A: Curry Recipe — Scenario A Fix-Verify

## Verdict: ✗ FAIL (B4-H1 fix path not exercised)

## Setup
- Agent topology: `default` + `specialist` (auto-edge `default ↔ specialist`)
- Input: `specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて`
- Runs: 1 run + 1 retry (both identical)

## dogfood_trace summary output

```
DOGFOOD TRACE SUMMARY
============================================================

[Skill Chain]  (0 workflow(s))

[Tool Calls]  (3 important tool call(s))
  [ 1] delegate_to_agent({"to": "specialist", "request": "カレーの簡単な作り方を教えて"})  caller=default
  [ 2] list_skills({"path": ""})  caller=specialist
  [ 3] list_skills({"path": "general"})  caller=specialist

[Peer Failures / Chain Discards]  (1 event(s))
  peer_reply_failed_surfaced: peer=specialist  reason=router completed without producing a text reply

[Interventions]  dispatch=0  resolve=0
[Agent Messages]  (2 message(s))

=== Cost Summary ===
  Total: $0.000661  |  6,447 tokens  |  4 calls
  Model: gemini-2.5-flash-lite
```

## dogfood_trace chain output

```
=== Skill / Tool Chain ===
[T+1.0s] tool: delegate_to_agent({"to": "specialist", ...})
[T+2.0s] tool: list_skills({"path": ""})
[T+3.0s] tool: list_skills({"path": "general"})
```

## Observed event sequence

1. `default` router: `delegate_to_agent → specialist` ✅
2. `specialist` router: `list_skills(path="")` → found category `general (10)` ✅
3. `specialist` router: `list_skills(path="general")` → found `direct_llm`, `eval`, `skill_improver`, etc. ✅
4. `specialist` router: returned `agent_response` **with no text** ✗
5. `default` router: `peer_reply_failed_surfaced` emitted ✗
6. Final output: `Could not get a result from agent 'specialist' (reason: router completed without producing a text reply)`

## Root cause analysis

The B4-H1 fix (`ffc9b4a`) routes `_run_skill_awaitable` narrator replies correctly to `agent_replies`.
However, this fix was **never triggered** because `specialist` never reached `invoke_skill`.

The model (gemini-2.5-flash-lite) discovered `direct_llm` via `list_skills` but violated the
system prompt rule:
> "After list_skills reveals at least one matching skill, Do NOT reply directly when a relevant skill
> is available; engage the skill ecosystem."

The model silently returned after discovery without invoking any skill or producing a text reply.

## New finding

**B5-H1 (HIGH)**: `specialist` router consistently fails to invoke skills after `list_skills` discovery.
gemini-2.5-flash-lite does not follow the consolidated commit-obligation rule despite explicit prompting.
The consolidation in `e90c0f2` may have weakened the signal — two rules merged into one paragraph
loses the `MUST` repetition that previously forced the model to act.

Possible mitigations:
- Restore explicit `MUST invoke_skill` rule as separate bullet (revert consolidation partially)
- Add a `max_turns` cap with explicit "you must invoke or explain" reminder in the intermediate turn
- Fallback: direct_llm auto-route when no skill invoked after list_skills result

## Expected checklist

| Check | Result |
|-------|--------|
| specialist invoke_skill 到達 | ✗ (list_skills only, never invoke_skill) |
| skill output → narrator reply → agent_replies → default | ✗ (never triggered) |
| default が user にカレーレシピを提示 | ✗ |
| `peer_reply_failed_surfaced` が出ない | ✗ (still emitted) |
