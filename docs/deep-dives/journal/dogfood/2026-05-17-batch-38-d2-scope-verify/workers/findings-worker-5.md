# B38 Worker 5 Findings — multi_agent_and_mcp.yaml

**Batch**: 38
**Worker**: 5 / 7
**HEAD**: `1d5042d` (fix(router): B38 D2-wrapper scope expansion)
**Run date**: 2026-05-17
**Port**: 8085
**Agent prefix**: `dogfood-b38-5`
**Scenario set**: `multi_agent_and_mcp.yaml` (7 scenarios)

---

## Summary

| Metric | B38 | B37 | Delta |
|--------|-----|-----|-------|
| Verified | 2 | 2 | 0 |
| Inconclusive | 1 | 2 | -1 |
| Refuted | 4 | 3 | +1 |
| Blocked | 0 | 0 | 0 |
| **V/I/R/B** | **2/1/4/0** | **2/2/3/0** | DeltaV=0 |

---

## CRITICAL: S3 Cold-Start Peer Retest (B38 Angle 1)

**B37 failure**: `invoke_action` called with `args={"message": "FP-0001の概要を要約してください。"}` — non-canonical arg. Root cause: description body had hardcoded `args={'message': <user_query>}` example.

**B38 fix**: Part A scope expansion (ARS block now covers all session-visible actions) + Part B description body hardcoded example removal.

### Description body excerpt (B38, from llm-tools-schema)

```
AGENT DELEGATION: For peer agent delegation, use action_name='agent.peer__<agent_name>'
with the canonical args shown in the ACTION ARG SCHEMAS block below (e.g. {request: ...}).
```

No `args={'message': <user_query>}` text. Hardcoded example: REMOVED.

### ARS block excerpt

```
ACTION ARG SCHEMAS (canonical keys for all session-visible actions):
  ...
  agent.peer__dogfood-b38-5-peer-researcher: {request}
  [97 total peer agents, all with {request}]
Use these exact key names in args when calling invoke_action.
```

### Actual tool_call event (S3)

```json
{
  "tool": "agent.peer__researcher",
  "args": {"request": "FP-0001の概要を要約してください。"}
}
```

B37 used: `message` (WRONG). B38 uses: `request` (CORRECT). Fix VERIFIED.

---

## B38 Angle 2: mcp.server__ / mcp.tool__ Scope

`mcp.server__*` and `mcp.tool__*` absent from ARS when no MCP servers configured — expected. Static ops include `mcp.operation__drop_server: {clear_secrets, scope, server}`. Dynamic entries appear only when sessions have configured servers (Source 3 in `_collect_all_session_ars_entries`). Absence correct.

---

## B38 Angle 3: W5 Peer Error Envelope Non-Regression

S3 error envelope: `{"status": "error", "kind": "agent_not_found", "error": "Agent 'researcher' not found in registry.", "available_agents": [...]}`. Non-regression VERIFIED.

---

## B38 Angle 4: mcp_install Misroute

S6: No routing_decided, inline reply. mcp_install->mcp_search misroute NOT observed (N=0, consistent with B36+B37).

---

## Per-Scenario Results

| ID | Verdict | Events | routing_decided | Key observation |
|----|---------|--------|-----------------|-----------------|
| mcp_search_registry | REFUTED | FAIL | 0 | list_actions(mcp.server)+inline, no alternatives |
| mcp_call_remote_tool | INCONCLUSIVE | PASS | 0 | inline reply only, explains no github MCP |
| agent_delegation_simple | VERIFIED | PASS | 1 | B38 fix: `request` arg used (canonical), not `message` |
| multi_agent_topology_route | VERIFIED | PASS | 3 | P1 researcher not found reported; P2 writer found via ARS, `request` canonical, 3-line summary produced |
| a2a_task_lifecycle_status_poll | REFUTED | PASS | 1 | Read README, said v1 no polling; didn't describe message/send shape or reyn web startup |
| mcp_install_permission_gate | REFUTED | FAIL | 0 | Inline clarification, no install attempt |
| cron_schedule_status | REFUTED | FAIL | 0 | Inline "feature not available", no route to ops_report |

---

## B38 Fix Verification Summary

| Fix | Status |
|-----|--------|
| Part A: ARS scope expansion | VERIFIED |
| Part B: Hardcoded `args={'message':}` removed | VERIFIED |
| S3 cold-start peer arg canonical | VERIFIED — `request` used |
| mcp.server__/mcp.tool__ in ARS when configured | NOT TESTED (no configured servers; absence correct) |
| W5 peer error envelope non-regression | VERIFIED |
| mcp_install misroute B35 4.4 | NOT observed (N=0) |

---

*Generated: 2026-05-17 | Worker 5 | B38 D2-scope-verify*
