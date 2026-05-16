# Dogfood B28 Worker 5 — Findings

**Scenario set**: `multi_agent_and_mcp.yaml`
**HEAD**: `f5a6866`
**Date**: 2026-05-17
**Worker**: 5/7

---

## C1 Verify: No Duplicate Function Declarations

All 7 scenarios verified clean. Each LLM call sees exactly 14 tools, no duplicates.

```
C1 OK traces/a2a_task_lifecycle.jsonl: 14 tools, no dupes
C1 OK traces/agent_delegation_simple.jsonl: 14 tools, no dupes
C1 OK traces/cron_schedule_status.jsonl: 14 tools, no dupes
C1 OK traces/mcp_call_remote_tool.jsonl: 14 tools, no dupes
C1 OK traces/mcp_install_permission_gate.jsonl: 14 tools, no dupes
C1 OK traces/mcp_search_registry.jsonl: 14 tools, no dupes
C1 OK traces/multi_agent_topology_route.jsonl: 14 tools, no dupes
```

---

## H3 Verify: agent.peer__ args translation fix (B27-H3)

**Result: PASS**

Scenario S4 (multi_agent_topology_route) triggered the relevant code path.

Event log excerpt (S4 run):
- tool_called: invoke_action with args={"action_name": "agent.peer__researcher", "args": {"message": "ReynのPhaseエンジンについて調べてください。"}}
- agent_message_sent: from_agent=dogfood-b28-5, to_agent=researcher, depth=0
- tool_returned: status=dispatched
- routing_decided: action_name=agent.peer__researcher, outcome=success

Trace excerpt (traces/multi_agent_topology_route.jsonl, response line 1):
  invoke_action args: {"action_name": "agent.peer__researcher", "args": {"message": "ReynのPhaseエンジンについて調べてください。"}}

Observations:
- LLM used "message" key (caller-side). B27-H3 fix translates to "request" (handler-side).
- agent_message_sent fired — dispatch reached OS delegation layer.
- tool_returned with status=dispatched — no KeyError, no crash.
- researcher agent does not exist at runtime => [error] agent 'researcher' not found (peer-not-found, not KeyError: 'request').
- NO B28-H3-REGRESS confirmed.

S3 (agent_delegation_simple): LLM passed string instead of object to invoke_action (invalid_args), so H3 code path not fully exercised there. No KeyError there either.

---

## Per-Scenario Results

### S1: mcp_search_registry — REFUTED
- Reply: "MCPレジストリでGitHub関連のサーバーを検索しましたが、該当するものが見つかりませんでした。"
- Events: tool_called(list_actions), tool_returned, tool_failed(search_actions: unknown_tool)
- Expected: routing_decided >=1, skill_run_spawned >=1 — NEITHER present
- Reply partially satisfies rubric ("no results found"), but required events missing.

### S2: mcp_call_remote_tool — REFUTED
- Reply: search_actions not available, mentions skill__mcp_search as option.
- Events: tool_failed(search_actions: unknown_tool)
- Expected: routing_decided >=1 — NOT present
- Actionable reply but required event missing.

### S3: agent_delegation_simple — INCONCLUSIVE
- Reply: "引数の形式が正しくないというエラーが発生しました。" — asks for clarification.
- Events: tool_failed(invoke_action: invalid_args), routing_decided — PRESENT
- routing_decided emitted. Reply doesn't clearly say "researcher agent does not exist" or explain creation. Borderline rubric compliance.

### S4: multi_agent_topology_route — INCONCLUSIVE
- Reply: "[error] agent 'researcher' not found" (first prompt), second prompt session ended.
- Events: tool_called(invoke_action), agent_message_sent, tool_returned(dispatched), routing_decided — PRESENT
- H3 fix confirmed working (see H3 Verify above).
- Multi-hop rubric not fully exercised (peers don't exist by design).

### S5: a2a_task_lifecycle_status_poll — REFUTED
- Reply: Describes available tools, says can't find reyn web / HTTP tools. Asks for more info.
- Events: must_emit: [] — satisfied
- Rubric: Does NOT describe message/send JSON-RPC, does NOT mention GET /a2a/tasks/{run_id}, does NOT explain reyn web must run. Misunderstands as tool invocation vs procedural how-to.

### S6: mcp_install_permission_gate — REFUTED
- Reply: "postgres MCP インストールアクション見つからず。mcp.operation__install_server あり、describe_action で確認しましょうか？"
- Events: chat_started, user_message_received, compaction_check, chat_stopped — no routing_decided, no skill_run_spawned
- Expected: routing_decided >=1, skill_run_spawned >=1 — NEITHER present

### S7: cron_schedule_status — INCONCLUSIVE
- Reply: "cron ジョブ一覧アクション見つからず。list_actions で exec カテゴリを検索してみましょうか？"
- Events: tool_called(invoke_action: exec__cron_list), tool_returned(error: no routing rule for 'exec'), routing_decided — PRESENT
- routing_decided present. Reply doesn't explain "no cron jobs configured" or mention reyn.yaml/CLI config path.

---

## Cross-Scenario Patterns

1. routing_decided gap for skill-targeting scenarios (S1, S2, S6): Router not dispatching via skill framework for MCP/install tasks. LLM calls list_actions or falls to direct tool calls instead of stdlib skills.

2. search_actions unknown_tool: Persistent B27 attractor seen in S1/S2. LLM attempts search_actions which doesn't exist.

3. A2A procedural question misrouted (S5): Informational how-to question treated as tool invocation request.

---

## Summary

V/I/R/B = 0/3/4/0
B27 worker 5 was V=0/I=1/R=6/B=0 → now I=3/R=4 (two scenarios improved from R to I: S3, S7)
H3 verify: PASS
C1 verify: PASS
