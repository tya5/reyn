# B32 Worker 5 Findings — multi_agent_and_mcp.yaml

**Run date**: 2026-05-17  
**HEAD**: c8fae2e  
**Agent**: dogfood-b32-5  
**Scenario set**: dogfood/scenarios/multi_agent_and_mcp.yaml (7 scenarios)

---

## Score Summary

| Scenario | ID | V/I/R/B | Notes |
|---|---|---|---|
| S1 | mcp_search_registry | I | First run found github servers; second run failed (unsafe python). Final reply was error. |
| S2 | mcp_call_remote_tool | I | History bleed from S1 contaminated context. No routing_decided. must_emit_any satisfied via chat_turn_completed_inline. No search_actions attractor. |
| S3 | agent_delegation_simple | I | routing_decided emitted; invoke_action args validation failed (string not object). Reply didn't explain agent-not-found or creation path. |
| S4 | multi_agent_topology_route | I | Researcher routing succeeded (routing_decided emitted, results returned). Writer routing not attempted — inline reply said writer action not visible. |
| S5 | a2a_task_lifecycle_status_poll | R | No mention of /a2a/tasks/{run_id} endpoint or message/send JSON-RPC method. must_emit empty (satisfied). Rubric not met. |
| S6 | mcp_install_permission_gate | R | No routing_decided, no skill_run_spawned. Replied "no capability." Both must_emit events missing. |
| S7 | cron_schedule_status | R | No routing_decided. Called list_actions(category=["mcp.server"]) first. Must_emit violated. Rubric not met. |

**Total**: V=0, I=4, R=3, B=0  
**vs B30 W5 (V=1, I=3, R=3, B=0)**: ΔV=-1, ΔI=+1 (net regression on V count; R stable)

---

## Key Findings

### F1: search_actions attractor RESOLVED (B32 verification angle)

**Observation**: S1 LLM called `invoke_action(action_name="skill__mcp_search", args={"query": "github"})` directly — no `search_actions(query=...)` hallucination.  
**Source**: Direct inspection of `/tmp/reyn-worktrees/b32-5/traces/mcp_search_registry.jsonl` — first response shows tool_call to `skill__mcp_search`.  
**Conclusion**: NEW-1+2 seed visibility fix resolved the B30 search_actions attractor for mcp_search routing.

### F2: mcp_search skill blocked by --allow-unsafe-python (S1)

The `mcp_search` skill declares a preprocessor step `./registry_fetch.py:fetch_registry_results` as unsafe python. Without `--allow-unsafe-python`, the skill fails. The first run of mcp_search completed with fabricated github server results (likely via LLM fallback), the second run failed with the permission error. Final reply was the error. This is a known constraint of the skill design.

### F3: History not wiped between scenarios — context contamination

The wipe recipe (`rm -rf .reyn/events .reyn/agents/dogfood-b32-5/events`) does NOT clear `history.jsonl`. S1→S2→S3→S4 ran with accumulated chat history. S2 first response was about S1's mcp_search failure (skill_completion_injected from S1 persisted into S2 session). Corrected wipe for S5-S7 included `rm -f .reyn/agents/dogfood-b32-5/history.jsonl`.

**Impact**: S2, S3, S4 may have lower-quality scores due to polluted context. S1, S5, S6, S7 were clean.

### F4: Agent peer delegation args validation failure (S3)

LLM called `invoke_action(action_name="agent.peer__researcher", args="\"...\"")` — the `args` field was a JSON-encoded string instead of an object. This caused `invalid_args` validation failure. Correct form: `args={"message": "..."}`. This is a recurring attractor where the LLM double-serializes the args object. routing_decided was emitted with outcome=error.

### F5: Writer agent not visible as seed alias (S4)

For researcher, the LLM successfully used `agent.peer__researcher` (presumably seeded). For writer, the LLM explicitly said "writer agent's specific action name is not available in the current tool list." This suggests `agent.peer__writer` is not seeded. The B30 NEW-1+2 fix seeded only the researcher agent alias (or whichever agents exist); writer agent doesn't exist, so no alias was seeded.

### F6: A2A, mcp_install, cron — no skill routing at all (S5, S6, S7)

All three scenarios resulted in inline replies with no skill routing attempts:
- S5 (a2a): LLM replied it "can't launch reyn web" without checking if ops-report or web__fetch could help
- S6 (mcp_install): Replied "no capability" without attempting mcp_install skill dispatch
- S7 (cron): Called `list_actions(category=["mcp.server"])` (irrelevant category), found nothing, replied "no tool found"

For S7, the LLM used wrong category (mcp.server vs cron/schedule) when probing list_actions. This is a category mismatch attractor.

---

## B30 Comparison

| Metric | B30 W5 | B32 W5 |
|---|---|---|
| Verified | 1 | 0 |
| Inconclusive | 3 | 4 |
| Refuted | 3 | 3 |
| Blocked | 0 | 0 |
| search_actions attractor | present | **resolved** |

B30's V=1 was scenario that is now I (likely due to history bleed or different seed state). R count stable at 3.

---

## Observations (unscored, for follow-up)

- **Wipe recipe gap**: B30-NEW-3 wipe recipe should include `rm -f .reyn/agents/<name>/history.jsonl`. Current recipe only clears events and state, leaving chat context contaminated between scenarios.
- **mcp_search unsafe-python**: The skill works as intended (routing happens) but blocked by flag. Dogfood harness should include `--allow-unsafe-python` for scenarios that exercise registry_fetch.
- **Args double-serialization**: LLM occasionally passes `args` as JSON string rather than object. Seen in S3. Possible envelope-layer fix (pre-validate args type in invoke_action schema).

