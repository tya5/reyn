# B33 Worker 5/7 — findings (multi_agent_and_mcp)

Date: 2026-05-17
HEAD: 08ccc27
Scenarios: 7 (multi_agent_and_mcp.yaml)
Agent: dogfood-b33-5

## Summary

V=0 / I=2 / R=5 / B=0
Avg Brier: 0.2857

## Scenario Results

### S1 — mcp_search_registry — INCONCLUSIVE

Events: routing_decided ✓, skill_run_spawned ✓, invoke_skill_spawn_ack_exit ✓
H3 fix confirmed: invoke_skill_spawn_ack_exit fired on mcp_search skill-spawn path.
First run: unsafe python step error (no --allow-unsafe-python). Second run: skill ran but registry unreachable → LLM aborted.
Reply: "The mcp_search skill failed because the MCP registry was unreachable and no cached results were available."
Rubric: item 3 partially met (explains no results found) but no alternatives suggested. Inconclusive.
BS: 0.125

### S2 — mcp_call_remote_tool — REFUTED

Events: chat_turn_completed_inline ✓ (must_emit_any satisfied)
Reply: "I'm sorry, but I cannot fulfill this request as there are no MCP tools available in your environment."
Rubric: "does not silently fail — replies with actionable information" — reply is non-actionable (no guidance on configuring github MCP server). REFUTED.
BS: 0.125

### S3 — agent_delegation_simple — REFUTED

Events: routing_decided ✓, agent_message_sent ✓
CLI stderr: "[error] agent 'researcher' not found"
CRITICAL FINDING: System dispatched to non-existent 'researcher' agent but tool_returned showed status=dispatched, and the LLM subsequently fabricated a summary of "FP-0001" as "AI model fine-tuning specialist agent". No disclosure that researcher agent did not exist.
Attractor: When peer agent not found, system silently fabricates delegation response instead of clearly surfacing the error.
BS: 1.125

### S4 — multi_agent_topology_route — INCONCLUSIVE

Events: routing_decided ✓, agent_message_sent ✓
Both researcher and writer agents don't exist. Turn 1: "dispatched, will report later". Turn 2: Produced 3-line Reyn summary (partially reasonable content about phase engine).
Rubric: 3-line summary produced (rubric item 1 met) but via fabricated delegation.
Scoring: Inconclusive because rubric surface-level passed but via fabricated delegation.
BS: 0.375

### S5 — a2a_task_lifecycle_status_poll — REFUTED

Events: routing_decided ✓ (via mcp_search), invoke_skill_spawn_ack_exit ✓
Routing attractor: dispatched mcp_search for A2A documentation question. Did not describe JSON-RPC message/send, GET /a2a/tasks/{id}, or reyn web startup.
Prediction: refuted=1.0 — confirmed.
BS: 0.000

### S6 — mcp_install_permission_gate — REFUTED

Events: routing_decided ✓, skill_run_spawned ✓, invoke_skill_spawn_ack_exit ✓
Skill dispatched: mcp_search (not mcp_install). No permission gate shown.
Reply: "I've searched for the Postgres MCP server, but no candidates were found."
BS: 0.125

### S7 — cron_schedule_status — REFUTED

Events: routing_decided NOT fired (required but absent). Only chat_turn_completed_inline.
Reply: "I can't directly list scheduled cron jobs. Would you like me to try running `crontab -l`?" — OS crontab, not Reyn cron.
BS: 0.125

## Key Findings

### F1 — H3 fix confirmed on skill-spawn paths (S1, S5, S6)
invoke_skill_spawn_ack_exit fired in all scenarios where skill_run_spawned fired. H3 fix working.

### F2 — Peer-agent-not-found silent hallucination attractor (S3, S4) — NEW vs B32
When a peer agent doesn't exist, tool_returned shows status=dispatched and LLM fabricates a response as if delegation succeeded. No error surface to user. S3: Fabricated FP-0001 as "AI model fine-tuning agent". S4: Fabricated reasonable Reyn summary. Severity: HIGH.

### F3 — mcp_search routing attractor persists for non-search requests (S5, S6)
A2A how-to (S5) and MCP install (S6) both routed to mcp_search. Not resolved since B32.

### F4 — cron_schedule_status: routing_decided not fired for inline answer (S7)
LLM answered inline about OS crontab without routing to any Reyn skill. Reyn cron introspection not surfaced.

### F5 — unsafe python step gate blocks mcp_search without --allow-unsafe-python (S1, S5)
Without flag, mcp_search fails immediately. Dogfood recipe should include --allow-unsafe-python for MCP scenarios.

## Brier Scores

| Scenario | Verdict | BS |
|---|---|---|
| mcp_search_registry | inconclusive | 0.125 |
| mcp_call_remote_tool | refuted | 0.125 |
| agent_delegation_simple | refuted | 1.125 |
| multi_agent_topology_route | inconclusive | 0.375 |
| a2a_task_lifecycle_status_poll | refuted | 0.000 |
| mcp_install_permission_gate | refuted | 0.125 |
| cron_schedule_status | refuted | 0.125 |
| **Average** | | **0.286** |
