# Dogfood Batch 27 — Worker 5 Findings
**Set**: multi_agent_and_mcp  
**Date**: 2026-05-17  
**Worker**: 5/7  
**Agent**: dogfood-b27-5  

---

## Verdict Matrix

| ID | Verdict | reply_pass | events_pass | artifacts_pass | Evidence one-liner |
|----|---------|-----------|-------------|----------------|--------------------|
| mcp_search_registry | inconclusive | false | true | n/a | skill_run_spawned fired but mcp_search failed: --allow-unsafe-python not provided |
| mcp_call_remote_tool | refuted | false | false | n/a | No skill_run_spawned; agent asked for args instead of explaining no github MCP configured |
| agent_delegation_simple | refuted | false | false | n/a | peer agent delegation KeyError('request'); no skill_run_spawned; vague error reply |
| multi_agent_topology_route | refuted | false | false | n/a | Same peer delegation KeyError on both hops; second prompt got no reply |
| a2a_task_lifecycle_status_poll | refuted | false | false | n/a | No skill_run_spawned; reply described generic HTTP ops, not A2A JSON-RPC or polling URL |
| mcp_install_permission_gate | refuted | false | false | n/a | No skill_run_spawned; flat refusal "I cannot install MCP servers" |
| cron_schedule_status | refuted | false | false | n/a | No skill_run_spawned; flat refusal "I do not have access to cron job listing" |

**V/I/R/B = 0/1/6/0**

---

## Per-Scenario Findings

### S1: mcp_search_registry — inconclusive

**Input**: "MCP レジストリで github 関連のサーバーを探して。"

**Stdout**:
```
[error] [mcp_search] failed: Skill 'mcp_search' declares an unsafe python step 
(./registry_fetch.py:fetch_registry_results) but --allow-unsafe-python was not provided.
```

**Events observed**:
- chat_started, user_message_received
- tool_called, skill_run_spawned  <- required event PRESENT
- tool_returned, skill_run_failed
- routing_decided x3, skill_completion_injected
- compaction_check, chat_stopped

**Assessment**: skill_run_spawned emitted (events_pass=true). mcp_search skill was correctly dispatched. Blocked by missing --allow-unsafe-python flag for registry_fetch.py preprocessor step. Reply does not satisfy rubric (no candidates listed, no alternatives offered). Verdict: inconclusive — routing reached skill but precondition prevented execution.

---

### S2: mcp_call_remote_tool — refuted

**Input**: "github MCP サーバーを使って最近の PR を一覧して。"

**Stdout**: Agent asked for required arguments for mcp.server__github() instead of explaining github MCP server is not configured.

**Events observed**: chat_started, user_message_received, compaction_check, chat_stopped. No skill_run_spawned, no tool_called.

**Assessment**: No skill routing. Reply fails both rubric paths. events_pass=false, reply_pass=false.

---

### S3: agent_delegation_simple — refuted

**Input**: "researcher エージェントに FP-0001 の概要を要約してもらって。"

**Key event evidence**:
```
tool_called: invoke_action(agent.peer__researcher, {message: "FP-0001 の概要を要約して"})
tool_failed: KeyError: 'request'
```

**Assessment**: Router attempted peer agent delegation correctly but invoke_action failed with KeyError: 'request' — bug in peer agent action handler. No skill_run_spawned. Vague error reply. events_pass=false, reply_pass=false.

**Root cause**: Peer agent invoke_action pathway has a KeyError: 'request' bug.

---

### S4: multi_agent_topology_route — refuted

**Input (multi-turn)**: researcher hop then writer hop.

**Events**: Same KeyError on researcher hop. Second prompt (writer) received but no tool_called, chat stopped. No skill_run_spawned at any point. Both hops failed.

---

### S5: a2a_task_lifecycle_status_poll — refuted

**Input**: "reyn web を起動して A2A エンドポイントにタスクを投げ、GET /a2a/tasks/<id> でステータスを確認するにはどうすればいい?"

**Stdout excerpt**: Generic HTTP description using mcp.tool__requests.post/get. Does not mention JSON-RPC message/send method name, specific endpoint shape, or that reyn web must run first.

**Events**: chat_started, user_message_received, compaction_check, chat_stopped. No skill_run_spawned.

---

### S6: mcp_install_permission_gate — refuted

**Stdout**: "I cannot fulfill this request. I do not have the capability to install MCP servers."

**Events**: No skill_run_spawned, no tool_called. Flat refusal.

---

### S7: cron_schedule_status — refuted

**Stdout**: "I cannot fulfill this request. I do not have access to functionality that lists scheduled cron jobs."

**Events**: No skill_run_spawned, no tool_called. Flat refusal.

---

## Root Causes Identified

1. **[BUG]** Peer agent invoke_action fails with `KeyError: 'request'` — blocks S3, S4.
2. **[PRECONDITION]** mcp_search requires `--allow-unsafe-python` for registry_fetch.py — blocks S1 completion.
3. **[ROUTING GAP]** MCP tool dispatch (S2), MCP install (S6), cron introspection (S7), A2A how-to (S5) produce flat refusals — suggests missing stdlib skills or skill catalog gaps.

## Calibration Check

Predicted refuted probability was 0.05 across all scenarios. Actual refuted = 6/7 (86%). Predictions significantly underestimated failure rate for this scenario set.
