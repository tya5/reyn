# B37 Worker 1 Findings — chat_router_smoke.yaml
Generated: 2026-05-17  
Branch: main HEAD 561101a  
Port: 8081 | Agent prefix: dogfood-b37-1-sN

---

## D2-wrapper description verify (primary B37 angle)

### Angle 1: ACTION ARG SCHEMAS block visible

Checked 2 LLM request payloads directly via dogfood_trace.py --mode llm-tools-schema:

**Request 10727bfb (S1, router, 06:04:18)** — invoke_action description excerpt:
```
ACTION ARG SCHEMAS (canonical keys for current hot-list actions):
  file__read: {path}
  file__list: {path}
  file__grep: {case_sensitive, glob, max_results, path, pattern}
  file__glob: {path, pattern}
  reyn.source__list: {path}
  web__search: {max_results, query}
  web__fetch: {max_length, url}
  memory.operation__remember_shared: {body, description, name, slug, type}
  skill__skill_builder: {description, goal, skill_name}
  skill__skill_improver: {_resolved_paths, case_input, case_name, ...}
Use these exact key names in args when calling invoke_action.
```

**Request b806c465 (S4, router, 06:05:33)** — invoke_action description excerpt:
```
ACTION ARG SCHEMAS (canonical keys for current hot-list actions):
  web__search: {max_results, query}
  file__read: {path}
  ...
  memory.operation__remember_shared: {body, description, name, slug, type}
Use these exact key names in args when calling invoke_action.
```

D2-wrapper block CONFIRMED in both inspected requests. Directly verified: 2/18 total LLM requests.

### Angle 2: D2-min/D2-full baseline non-regression

All direct hot-list tool parameters.properties non-empty (request 10727bfb):
- file__read: [path], file__grep: [pattern,path,glob,case_sensitive,max_results]
- web__search: [query,max_results], web__fetch: [url,max_length]
- All 10 direct hot-list tools confirmed non-empty.

**D2-min/D2-full baseline CONFIRMED.**

### Angle 3: invoke_action wrapper-path arg-name analysis

4 invoke_action tool_calls observed from LLM responses:

| Request | Action name | Args | Canonical? |
|---------|------------|------|-----------|
| 030a8544 (S2 T2 attempt 1) | default_api.web__search | {query, max_results} | WRONG name (hallucinated) |
| c895cce9 (S2 T2 attempt 2) | web__search | {max_results, query} | CORRECT |
| b806c465 (S4) | skill__word_stats_demo | {text} | CORRECT |
| 2bf460ec (S6 T2) | default_api.web__search | {query, max_results} | WRONG name |

S4 and S2-retry used canonical names. S2-first and S6 used 'default_api.web__search' — see B37-OBS-1 below.

---

## Per-Scenario Results

### S1: simple_capability_question — REFUTED
Reply: "Reyn エージェントです。Reyn プロジェクトのコンテキストで利用可能なアクションを検索できます。どのような機能にご興味がありますか?"
- Rubric 1 (mentions skills/agents/capabilities): PARTIAL — mentions エージェント and アクション but does not explicitly list skills or describe skill capabilities
- Rubric 2 (helpful, no error): PASS
- user_message_received: emitted; permission_denied: NOT emitted
- No direct_llm artifact (inline router reply)
- Verdict: REFUTED — reply vague, no skill artifact

### S2: factual_query_direct_llm — VERIFIED
Reply: "「冪等」（べきとう）とは、ある操作を1回行っても、複数回行っても、その結果が同じであることを意味する数学用語です。...HTTPの PUT や DELETE メソッドは冪等性を持つ..."
- Rubric (explains idempotency factually): PASS
- permission_denied: NOT emitted; routing_decided: 3x emitted
- Final web_search via invoke_action(web__search) succeeded with args {max_results:1, query:"冪等とは"}
- Verdict: VERIFIED

### S3: skill_discovery_request — REFUTED
Reply: 13 skills listed (skill__direct_llm through skill__skill_improver)
- Rubric 1 (mentions skill name): PASS; Rubric 2 (no error): PASS
- Must_emit routing_decided: NOT emitted — used list_actions(category=['skill']) → inline reply
- Consistent with B36 prediction (refuted=1.0)
- Verdict: REFUTED (structural event miss, consistent with prediction)

### S4: explicit_skill_invocation_word_stats — VERIFIED
Reply: "determined it to be 44 characters, 5 words, and 1 line long"
- Rubric (word count/char count statistics): PASS
- routing_decided emitted (invoke_action, skill__word_stats_demo)
- skill_run_spawned emitted (run_id: 20260517T060540Z_word_stats_demo_a9ab)
- skill_run_completed emitted (status: finished)
- permission_denied: NOT emitted
- Artifact: word_stats_demo present at .reyn/artifacts/word_stats_demo/review/v01_text_review.json
- invoke_action call: action_name='skill__word_stats_demo', args={text:'...'} — canonical
- Verdict: VERIFIED

### S5: catalog_routing_decided_emitted — INCONCLUSIVE
T1: Asked clarifying question ("どのようなテーマや雰囲気の詩がお好みですか?")
T2: "風がそっと 髪を撫でる / 木漏れ日が 葉を踊らせる / 静かな午後 わたしの心にも / 優しい時間が流れていく"
- Rubric 1 (contains poem): PASS (4-line poem after follow-up)
- Rubric 2 (natural poetic form ≥2 lines): PASS
- must_emit_any (routing_decided OR chat_turn_completed_inline): chat_turn_completed_inline present — PASS
- permission_denied: NOT emitted
- Verdict: INCONCLUSIVE — poem produced but required extra clarification turn; strict single-turn scenario assumption broken

### S6: multi_turn_pronoun_reference — VERIFIED
T1 reply: explained list comprehension with code example
T2 reply: "squares = [x**2 for x in range(10)]... long_words = [word for word in words if len(word) >= 6]..."
- Rubric 1 (T2 contains Python list comp code): PASS
- Rubric 2 (coherent with T1 topic): PASS
- permission_denied: NOT emitted
- T2 tool call: invoke_action(default_api.web__search) → error → agent fell back to LLM knowledge
- routing_decided emitted (source: invoke_action, action: default_api.web__search)
- Verdict: VERIFIED (reply correct despite failed tool call)

### S7: out_of_scope_graceful_decline — VERIFIED
Reply: "申し訳ありませんが、私は画像を生成する機能を持っていません。"
- Rubric 1 (politely declines): PASS
- Rubric 2 (doesn't pretend to generate): PASS
- permission_denied: NOT emitted
- Verdict: VERIFIED

---

## Score Summary

| ID | Scenario | Verdict |
|----|----------|---------|
| S1 | simple_capability_question | REFUTED |
| S2 | factual_query_direct_llm | VERIFIED |
| S3 | skill_discovery_request | REFUTED |
| S4 | explicit_skill_invocation_word_stats | VERIFIED |
| S5 | catalog_routing_decided_emitted | INCONCLUSIVE |
| S6 | multi_turn_pronoun_reference | VERIFIED |
| S7 | out_of_scope_graceful_decline | VERIFIED |

**V/I/R/B = 4/1/2/0**

---

## B37-OBS-1: default_api.web__search as spurious tool in toolset

**Observation (primary data, events log + LLM trace):**

Three LLM requests (d8775949/ae44217a for S3, 2bf460ec for S6 T2) received `default_api.web__search` as an actual tool definition:
- description: "Direct alias for default_api.web__search. Use invoke_action for schema details."
- parameters.properties: {} (empty — D2-min issue for this alias)

This is a non-canonical qualified name (valid Reyn form: `web__search`, not `default_api.web__search`).

**Effect observed:**
- S6 T2: LLM tried invoke_action(action_name='default_api.web__search') → Unknown action error → graceful fallback to LLM knowledge → correct reply
- S2 T2 attempt 1 (request 030a8544): LLM hallucinated 'default_api.web__search' even though it was NOT in that request's toolset → error → self-corrected to canonical 'web__search'

**Hypothesis:** action_usage.jsonl (hot_list) may have recorded 'default_api.web__search' from a prior session's miscall, and the hot_list construction is re-exposing this malformed name. Observation only — no source investigation performed.

---

## Cost
18 LLM calls total, $0.009392, 92,794 tokens (gemini-2.5-flash-lite)
