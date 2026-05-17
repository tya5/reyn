# B39 Worker 5 Findings — multi_agent_and_mcp.yaml

**Batch**: 39
**Worker**: 5 / 7
**HEAD**: `b4daeb1` (fix(stdlib): explicit input_schema for empty-schema skills)
**Run date**: 2026-05-17
**Port**: 8085
**Agent prefix**: `dogfood-b39-5`
**Scenario set**: `multi_agent_and_mcp.yaml` (7 scenarios)

---

## Summary

| Metric | B39 | B38 | Delta |
|--------|-----|-----|-------|
| Verified | 1 | 2 | -1 |
| Inconclusive | 2 | 1 | +1 |
| Refuted | 4 | 4 | 0 |
| Blocked | 0 | 0 | 0 |
| **V/I/R/B** | **1/2/4/0** | **2/1/4/0** | DeltaV=-1 |

---

## B39 Angle 1: S3 Cold-Start Peer Canonical Non-Regression

**B38 verified**: `request` arg canonical (not `message`). B39 should hold.

### Actual tool_call event (S3)

```json
{
  "tool": "invoke_action",
  "args": {
    "action_name": "agent.peer__dogfood-b39-5-peer-researcher",
    "args": {"request": "FP-0001 の概要を要約してください。"}
  }
}
```

**arg_canonical**: `request` used. Non-regression HELD.

Note: peer-researcher agent existed (created by worker setup), so delegation succeeded and peer replied. Rubric met (substantive content from delegate).

---

## B39 Angle 2: mcp_install Misroute (N=4 confirm?)

S6: `chat_turn_completed_inline` (tool_calls_attempted=0), routing_decided=0. mcp_install→mcp_search misroute NOT observed. N=0 in B36/B37/B38/B39 = 4 batches non-recurrence. Confirmed.

---

## B39 Angle 3: W5 Peer Error Envelope Non-Regression

S3: Peer delegation succeeded (agent existed, no error envelope). S4 P2: `invoke_action({action_name:"writer"})` failed gracefully (no bad envelope). Error envelope format non-regression: NOT directly tested (error path not triggered). Confidence: inference only.

---

## B39 Angle 4: Ghost Peer Agents Rejected from Hot-List

**Ghost filter fix** (bfeb9f8): Removes aliases from hot-list that don't exist in current session registry.

**Observation (S4)**: peer-researcher LLM called `search_actions(query="Reyn Phase engine")` which returned `agent.peer__b16_s2`, `agent.peer__b23_s2`, etc. — old ghost agents appearing in semantic search. These did NOT appear as hot_list_alias routing_decided events.

**Key distinction**: Ghost filter applies only to hot-list aliases. `search_actions` semantic results are not filtered. Old agents remain reachable via semantic search path.

**Hot-list ghost filter**: VERIFIED for its scope. No old ghost agents in `source=hot_list_alias` routing.

---

## B39-Specific: stdlib input_schema fix (b4daeb1)

`user_message.yaml` now present in `direct_llm/artifacts/` and `read_local_files/artifacts/`, broadening ARS coverage. Effect NOT directly observable in W5 scenarios — S1/S6/S7 still produced inline replies.

---

## S2 Notable: search_actions hallucination

S2: LLM called `search_actions` (tool_failed: unknown_tool) before falling back to `list_actions`. Same hallucination observed in S4 peer-researcher sub-agent. Not a regression (graceful failure), but recurring LLM hallucination vector across agents.

---

## Per-Scenario Results

| ID | Verdict | Events | routing_decided | Key observation |
|----|---------|--------|-----------------|-----------------|
| mcp_search_registry (S1) | REFUTED | FAIL | 0 | list_actions(mcp.server, filter=github) empty → inline. Same as B38. |
| mcp_call_remote_tool (S2) | INCONCLUSIVE | PASS | 0 | search_actions hallucination + list_actions(github) empty → inline. PASS via chat_turn_completed_inline. |
| agent_delegation_simple (S3) | VERIFIED | PASS | 2 | request arg canonical (HELD). Peer existed, responded. |
| multi_agent_topology_route (S4) | INCONCLUSIVE | PASS | 6 | P1: peer-researcher dispatched (request canonical). P2: writer invoke_action({action_name:"writer"}) bad call (no args). No 3-line summary. |
| a2a_task_lifecycle_status_poll (S5) | REFUTED | PASS | 1 | routing_decided=1 via web__search error. Didn't describe message/send or reyn web startup. |
| mcp_install_permission_gate (S6) | REFUTED | FAIL | 0 | Inline clarification, no tool call. mcp_install misroute NOT observed. |
| cron_schedule_status (S7) | REFUTED | FAIL | 0 | Inline "機能は提供されていません". No routing. Same as B38. |

---

## Delta vs B38 Explanation

S3 held VERIFIED. S4 regressed VERIFIED→INCONCLUSIVE: B38 S4 had researcher not found + writer found via ARS → 3-line summary produced. B39 S4 P2: `invoke_action({action_name:"writer"})` missing args field → routed back to researcher instead of writer. Net: -1V (S4), +1I (S4 reclassification). S2 remained INCONCLUSIVE in both batches.

---

## B39 Fix Verification Summary

| Fix | Status |
|-----|--------|
| Ghost hot-list filter (bfeb9f8) | VERIFIED — no old ghost agents in hot_list_alias routing |
| stdlib input_schema fix (b4daeb1) | NOT DIRECTLY OBSERVABLE in W5 scenarios |
| S3 cold-start peer arg canonical (`request`) | HELD — non-regression confirmed |
| mcp_install misroute non-recurrence | N=4 batches (B36/B37/B38/B39) all N=0 |
| B39 #119 empty-schema false-ghost fix (b1ca51a) | VERIFIED indirectly (peer-researcher sub-agent used empty-schema skills without ghost rejection) |

---

*Generated: 2026-05-17 | Worker 5 | B39 b39-fix-wave-verify*
