# B38 Worker 1 Findings

Batch: B38 | Worker: 1/7 | Date: 2026-05-17
HEAD: 1d5042d | Port: 8081 | Agent prefix: dogfood-b38-1-sN
Verdicts: V=4, I=0, R=3, B=0 | DeltaVsB37: S1 R->V, S5 I->R, S7 possible V->R regression

## ARS Scope Verify (PRIMARY)

Description length: 3991 chars (commit claimed 3080; actual higher due to 8 peer agents)
Hardcoded message example removed: CONFIRMED (args={'message':} not found in description)

ACTION ARG SCHEMAS block observed (req 641c49cc, S1 first call):
  exec__sandboxed_exec, file__delete, file__glob, file__grep, file__list, file__read,
  file__write, mcp.operation__drop_server, memory.operation__forget,
  memory.operation__remember_agent, memory.operation__remember_shared,
  rag.operation__drop_source, rag.operation__recall, reyn.source__list, reyn.source__read,
  web__fetch, web__search, skill__eval, skill__eval_builder, skill__index_docs,
  skill__index_events, skill__judge_phase, skill__ops_report, skill__skill_builder,
  skill__skill_improver, agent.peer__default, agent.peer__dogfood-b38-1,
  agent.peer__dogfood-b38-1-s2..s7 (8 peer entries)

Total ARS entries: 33
file__write: PRESENT
rag.operation__drop_source: PRESENT
agent.peer__ entries: PRESENT (8 entries)
mcp.tool__: ABSENT (no MCP tools installed in this session — expected)
mcp.operation__drop_server: PRESENT

## Ghost Rejection Verification

web.log (every turn): "[reyn] action_usage: skipping invalid alias 'web_search' — not in current action registry"
17 LLM requests examined: zero default_api.* or skill__create_skill in any toolset.
B37-OBS-1 (default_api.web__search spurious tool): RESOLVED.
Direct alias schemas non-empty: skill__skill_builder 3 props, skill__skill_improver 9 props.

## Scenario Results

S1 simple_capability_question: V — Reply names skills explicitly (list_actions, invoke_action, file__*, web__*, skill__skill_builder). Inline reply, no direct_llm artifact. B37 was R (vague). Improvement.

S2 factual_query_direct_llm: V — Correct idempotency explanation. Unexpected routing: ghost web_search rejected, then invoke_action(agent.peer__dogfood-b38-1-s7). S7 peer did web__search('冪等とは'), returned results. Final S2 reply correct. routing_decided(agent.peer) emitted.

S3 skill_discovery_request: R — Reply lists 16 skills correctly (PASS). routing_decided NOT emitted (used list_actions inline). Consistent with B37 W1 S3 and outcome_prediction (refuted=1.0).

S4 explicit_skill_invocation_word_stats: V — invoke_action(skill__word_stats_demo) canonical. skill_run_spawned, skill_run_completed(finished), routing_decided all emitted. Artifacts created. 44 chars/1 line/~10 tokens reported.

S5 catalog_routing_decided_emitted: R — Asked for poem theme/atmosphere instead of writing poem ('どのようなテーマや雰囲気の詩をご希望ですか？'). chat_turn_completed_inline emitted (must_emit_any satisfied). Reply rubric FAIL. B37 was I.

S6 multi_turn_pronoun_reference: V — Turn 1 inline explanation. Turn 2 resolved 'それを' to list comprehension topic, produced code examples (squares, even_squares). web__search + web__fetch (graceful failure). No permission_denied.

S7 out_of_scope_graceful_decline: R — Asked 'どのような画像を生成しますか？' instead of declining. Reply rubric FAIL. B37 W1 S7 was V (clean polite decline). Possible regression.

## New Findings

B38-OBS-1 (LOW): S5 clarification attractor without poem. B37 was I (clarification then poem); B38 no poem in single turn. Same attractor.

B38-OBS-2 (MED): S7 image not declined — possible regression. B37 V -> B38 R. Pre-conclusion checklist: N=1 observation, cannot confirm as consistent regression. Hypothesis: ARS description expansion (3991 chars) may reduce signal strength for implicit capability constraints. Needs N>=3 retest.

B38-OBS-3 (LOW): S2 peer delegation side effect. ARS scope expansion made agent.peer__s7 visible as a selectable target. S7 agent context contaminated with idempotency history before receiving image-generation request — may relate to B38-OBS-2.

## Cost

Total: $0.012801 | 125,527 tokens | 18 calls | gemini-2.5-flash-lite (17), openai/gemini-2.5-flash-lite (1)
