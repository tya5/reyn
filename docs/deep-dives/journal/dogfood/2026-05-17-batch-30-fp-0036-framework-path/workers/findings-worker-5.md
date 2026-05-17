# Dogfood B30 Worker 5 — Findings

**Batch**: B30  **Worker**: 5/7  **Scenario set**: `multi_agent_and_mcp.yaml`
**HEAD**: `4be42fe` (Merge feat/b29-q2-synthetic-event)
**Run date**: 2026-05-17
**Previous batch**: B28 Worker 5 (V/I/R/B = 0/3/4/0)

---

## B29-Q2 Verify: chat_turn_completed_inline

Fix scope: Emit `chat_turn_completed_inline` for inline LLM replies (pre-flight refusal path).
Scenarios targeted: S2 (mcp_call_remote_tool), S5 (a2a_task_lifecycle_status_poll).

### S2 — mcp_call_remote_tool
- Event: `chat_turn_completed_inline` count=1, decision=inline_reply, tool_calls_attempted=1
- `must_emit_any: [routing_decided, chat_turn_completed_inline]` SATISFIED by chat_turn_completed_inline
- **Q2 fix verified for S2**

### S5 — a2a_task_lifecycle_status_poll
- Event: `chat_turn_completed_inline` count=1, decision=inline_reply, tool_calls_attempted=2
- `must_emit: []` trivially satisfied; additionally chat_turn_completed_inline present
- **Q2 fix verified for S5**

**B29-Q2 summary: 2/2 confirmed**

---

## C1 Verify: No Duplicate Declarations
- `reyn skills validate --all`: 17 skills, 0 errors, 0 warnings
- Name uniqueness check across skill.md files: no duplicates found
- **C1 PASS**

---

## Scenario Verdicts

### S1: mcp_search_registry — INCONCLUSIVE
Events: routing_decided x2, skill_run_spawned x1 (PASS)
- skill__mcp_search spawned then failed: "Skill 'mcp_search' declares an unsafe python step (./registry_fetch.py:fetch_registry_results) but --allow-unsafe-python was not provided"
- LLM fabricated "github" and "github_actions" results before receiving error notification
- Final narration: explained it failed with unsafe-python error
- Rubric: partial (error explained, no results listed, no alternatives suggested)
- Environmental blocker (unsafe-python) prevents full rubric validation

### S2: mcp_call_remote_tool — REFUTED
Events: chat_turn_completed_inline x1 (PASS via must_emit_any)
- LLM called search_actions(query="github MCP server recent PRs") -> unknown_tool error
- Reply: "I'm sorry, I cannot fulfill this request. The `search_actions` tool is not available in the current environment."
- Rubric fails point 1: reply explains search_actions unavailable, not that github MCP is not configured
- B29-Q2 fix confirmed: chat_turn_completed_inline emitted
- search_actions attractor (B28 W5 finding) persists

### S3: agent_delegation_simple — INCONCLUSIVE
Events: routing_decided x2 (PASS)
- router dispatched to agent.peer__researcher twice; CUI: [error] agent 'researcher' not found
- LLM reply: "I have asked the researcher agent to summarize FP-0001 for you. Please wait for its response."
- Reply does not convey agent-not-found; doesn't explain how to create agent
- OS correctly dispatched; LLM narration failure (not OS failure)

### S4: multi_agent_topology_route — VERIFIED
Events: routing_decided x2 (PASS)
- Turn 1: dispatched to researcher (not found). LLM internally fabricated researcher context.
- Turn 2: dispatched to writer (not found). LLM self-synthesized 3-line summary:
  "Reyn は、Markdown DSL を使用して LLM フェーズ実行を可能にするシステムです。
   クローズドな候補遷移、JSON スキーマ検証、フェーズごとの権限スコープにより、LLM の自律性を制限します。
   これにより、安全で管理された LLM アプリケーション開発が実現されます。"
- Rubric satisfied: concise 3-line summary produced, not empty

### S5: a2a_task_lifecycle_status_poll — REFUTED
Events: must_emit: [] (trivially PASS); chat_turn_completed_inline emitted
- LLM called list_actions x2 looking for HTTP tools
- Reply: explained web__search/web__fetch tool limitations; suggested external requests library
- All 3 rubric points fail (no A2A method description, no GET /tasks/{id} mention, no reyn web instruction)
- B29-Q2 fix confirmed: chat_turn_completed_inline emitted

### S6: mcp_install_permission_gate — INCONCLUSIVE (was REFUTED in B28)
Events: routing_decided x2, skill_run_spawned x1 (PASS)
- mcp_search spawned (failed: unsafe-python, same as S1)
- mcp.server__install action also routed (routing_decided shows action_name=mcp.server__install)
- Reply: "MCPサーバーの検索中にエラーが発生しました。安全でないPythonコードの実行が許可されていなかった"
- Rubric: install was attempted (partial pass on point 1); permission gate (layer-4) not reached due to unsafe-python blocker
- Promoted R->I: install routing happened

### S7: cron_schedule_status — REFUTED
Events: routing_decided x1 (PASS)
- LLM found exec__sandboxed_exec via list_actions
- Executed crontab -l -> sandbox blocked: "sandbox-exec: execvp() of 'crontab' failed: Operation not permitted" (returncode=71)
- Reply: "cron ジョブの一覧を表示しようとしましたが、権限がないため実行できませんでした。"
- Wrong tool path: should have used reyn cron list / ops_report, not system crontab
- Rubric fails: reply doesn't say "no cron jobs configured"; doesn't show reyn.yaml/CLI guidance

---

## search_actions Attractor Status

B28 W5 finding: LLM calls search_actions (not in catalog) when asked about MCP/tool discovery.

B30 observation: Attractor in S2 only (1/7 scenarios).
- S2: search_actions(query="github MCP server recent PRs") -> unknown_tool error
- S1, S3-S7: correct tools used (invoke_action, list_actions, sandboxed_exec, agent.peer__)

Direct evidence (S2 trace 4d0bc659): tool_calls=[{name: search_actions, args: {query: "github MCP server recent PRs"}}]

Pre-conclusion 5Q: (1) Specific: S2 trace direct inspection. (2) Primary data. (3) Falsification: checked all 7/7. (4) Infra: trace JSONL captured. (5) 7/7 inspected. Conclusion: attractor is scenario-conditional; present for github+MCP+PR phrasing in S2, absent in S1/S3-S7.

B28-MED-1 fix (index_docs seed) did not eliminate attractor for S2 phrasing. The attractor persists.

---

## Summary

| # | Scenario | Verdict | B28 | Delta |
|---|----------|---------|-----|-------|
| S1 | mcp_search_registry | I | I | = |
| S2 | mcp_call_remote_tool | R | R | = |
| S3 | agent_delegation_simple | I | I | = |
| S4 | multi_agent_topology_route | V | I | +1 |
| S5 | a2a_task_lifecycle_status_poll | R | R | = |
| S6 | mcp_install_permission_gate | I | R | +1 |
| S7 | cron_schedule_status | R | R | = |

B30 W5: V/I/R/B = 1/3/3/0
B28 W5: V/I/R/B = 0/3/4/0
Delta: +2

---

## Open Issues

| ID | Sev | Description |
|----|-----|-------------|
| B30-W5-1 | MED | search_actions attractor in S2 persists post-B29 |
| B30-W5-2 | MED | S5: LLM answers tool-capability question instead of A2A architecture how-to |
| B30-W5-3 | MED | S7: LLM routes to system crontab -l instead of reyn cron infrastructure |
| B30-W5-4 | LOW | S3: LLM narration does not convey agent-not-found error to user |
| B30-W5-5 | LOW | mcp_search requires --allow-unsafe-python; blocks S1/S6 from reaching intended paths |
