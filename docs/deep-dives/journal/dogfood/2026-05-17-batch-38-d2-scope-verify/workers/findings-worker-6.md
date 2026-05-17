# B38 Worker 6 Findings

Batch: B38 / Worker: 6/7 / Date: 2026-05-17 / HEAD: 1d5042d
Agent prefix: dogfood-b38-6 / Port: 8086 / Worktree: /private/tmp/reyn-worktrees/b38-6

## Totals

V/I/R/B = 3/5/3/0
Delta vs B37 W6: B37={V:4,I:3,R:4,B:0} -> B38={V:3,I:5,R:3,B:0}
Net: -1V, +2I, -1R, 0B

## R-WEB Unblock Retest

All 3 B37-REFUTED mcp_search scenarios (narr-1, s-fp11-3, s-fp12-completion-1) now emit:
  routing_decided {action_name: skill__mcp_search, source: invoke_action, outcome: success}
  followed by skill_run_spawned -> workflow_aborted (MCP registry unreachable in dogfood env)

B37 had zero tool calls on these scenarios (routing miss). B38 dispatches correctly.
Hot-list seed expansion confirmed. permission_denied: ABSENT in all 3.
r_web_scenarios_verified=0 (registry unreachable), r_web_routing_gap_closed=3.

## D2-Wrapper Scope Verify

_collect_all_session_ars_entries returns 19 entries. ARS header: "all session-visible actions".
Plan steps used invoke_action for file reads without arg hallucination.
hot_list_alias source confirmed for skill__skill_builder (s-fp12-spawn-1).

## Plan Emitted: 2/3

plan_compare: V->I (same as B37). plan_explain: B37=V -> B38=R (N=1 regression). plan_summary: R (same).

## W6 phase_no_progress fix: non-regression CONFIRMED

s-fp11-1 traversed phase_no_progress {build_skill, verify_skill} -> workflow_aborted -> skill_completion_injected -> reply captured.
