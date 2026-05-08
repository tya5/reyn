# B11-R3: Router Text-Reply Fix Verification

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD post-fix | (see commit below) |
| Fix | `src/reyn/chat/router_system_prompt.py` Behaviour rule restructure |
| Verdict | **verified** |
| Pre-fix failure rate | 3/5 = 60% (2 text-reply + 1 empty response) |
| Post-fix failure rate | 0/2 confirmed = 0% (2 successful invoke_skill dispatches) |

## Fix Applied

File: `src/reyn/chat/router_system_prompt.py`

Changes to Behaviour rules:

### Pre-fix (old rules):
```
- Reply directly only for chitchat, questions about yourself,
  and clarifications back to the user. Domain tasks → Action.
- For Action or explicit-skill requests, call list_skills first,
  then invoke_skill (use describe_skill in between only when you need to inspect).
- If the user names a skill, use list_skills + invoke_skill
  rather than paraphrasing the request as a Reply.
```

### Post-fix (new rules):
```
- Reply directly only for chitchat and questions about yourself.
  Domain tasks → Action. Do NOT ask clarifying questions if a skill name
  from the Available skills list appears in the user message — treat it as Action.
- If the user names a skill that appears in the Available skills list,
  call invoke_skill directly (skip list_skills). Any other entities
  in the user message are inputs to the skill, NOT reasons to clarify.
- If the skill name is NOT in the Available skills list above,
  call list_skills first, then invoke_skill.
```

## Post-Fix Sessions

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

### Post-Fix Session 1

```
Events:
  user_message_received: skill_improver で direct_llm を 1 回 review して改善案を出して
  tool_called: invoke_skill        ← direct, no list_skills first
  workflow_started: skill_improver ← chain started immediately
  ... (chain progressing)
```

Router called `invoke_skill` directly. No text reply. No `list_skills` call. Chain started.

### Post-Fix Session 2

Session killed at 12s. Only 3 events (chat_started, user_message_received, chat_stopped). Ambiguous — background process was killed too quickly for the router to complete the first LLM call.

### Post-Fix Session 3

```
Events (155 total):
  user_message_received: skill_improver で direct_llm を 1 回 review して改善案を出して
  tool_called: invoke_skill        ← direct, no list_skills first
  workflow_started: (multiple)     ← skill chain executing
  tool_called: run_skill           ← sub-skills executing
  ... (155 events, chain progressing significantly)
```

Router called `invoke_skill` directly. 155 events confirm extensive chain execution.

## Test Suite

Full test suite: **1011 passed** (1010 baseline + 1 new Tier 3 test), 2 xfailed.

### Tests Modified

- `tests/test_router_system_prompt.py::TestBehaviourRulesAfterF3F9Fix::test_explicit_skill_name_directs_to_invoke`
  - Updated assertions to match new text ("invoke_skill directly", "inputs to the skill")
  - Old assertions checked for "list_skills + invoke_skill" and "paraphrasing" (both removed)

### Tests Re-Recorded (system prompt change → SHA-256 key mismatch)

LLMReplay fixtures keyed on model + messages (including system prompt). System prompt change invalidated 3 existing fixture keys:

- `tests/fixtures/llm/router/chitchat.jsonl` — re-recorded
- `tests/fixtures/llm/router/invoke_skill_single_round.jsonl` — re-recorded
- `tests/fixtures/llm/router/memory_recall.jsonl` — re-recorded

All 3 re-recorded tests pass in replay mode.

### New Test Added (Tier 3 LLMReplay)

File: `tests/test_replay_skill_router.py::test_named_skill_direct_invoke_without_list_skills`

```
Tier 3: B11-R3 fix — when user names a skill that appears in the
Available skills list, router calls invoke_skill directly (no list_skills hop).
```

Fixture: `tests/fixtures/llm/router/named_skill_direct_invoke.jsonl`

Test verifies:
1. `host.skill_calls >= 1` — router invoked a skill (not text reply)
2. `host.skill_calls[0]["skill"] == "skill_improver"` — correct skill invoked

This test would fail if the B9-NEW-3 regression recurred (LLM produces text reply instead of tool call).

## Verdict

**verified**: The structural fix eliminates the "clarification escape" path that caused text-reply non-determinism. Both confirmed post-fix sessions showed direct `invoke_skill` dispatch without the intermediate `list_skills` discovery step.

The fix aligns with `feedback_reyn_care_boundary.md` structural environment principle: we improved the LLM's structural context (pre-call rules), not its judgment (we don't tell it what the user "really wants").

## Residual Risk

The fix reduces but may not eliminate the text-reply pattern in edge cases:
- Users whose input contains a skill name that is not in Available skills (list_skills path still required)
- G12 attractor variant (empty stop) is a separate pattern — partially reduced because fewer tool calls are needed (no mandatory list_skills hop), but this is not fully addressed by this fix
- Very ambiguous inputs where even the improved rules don't disambiguate clearly

Rate target (from batch 11 prelude): 50% → ~10% or less. Observed: 0/2 post-fix = 0% (N too small for statistical confidence). Step 2 integration retest (5-shot) will measure more precisely.
