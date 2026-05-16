# Batch 27 Worker 3 — Findings: control_ir_ops

**Date:** 2026-05-17  
**Agent:** dogfood-b27-3  
**Worktree:** /tmp/reyn-worktrees/b27-3  
**Scenario set:** dogfood/scenarios/control_ir_ops.yaml (9 scenarios)

---

## Verdict Matrix

| id | reply_pass | events_pass | artifacts_pass | verdict |
|----|-----------|-------------|----------------|---------|
| file_read_via_chat | PASS | FAIL | FAIL | inconclusive |
| file_glob_grep | FAIL | FAIL | FAIL | refuted |
| web_search_query | PASS | FAIL | FAIL | inconclusive |
| web_fetch_url | PASS | FAIL | FAIL | inconclusive |
| sandboxed_exec_simple | FAIL | FAIL | FAIL | refuted |
| lint_a_skill | FAIL | FAIL | FAIL | refuted |
| recall_indexed_source | FAIL | FAIL | FAIL | blocked |
| judge_output_direct | FAIL | FAIL | FAIL | blocked |
| ask_user_round_trip | FAIL | FAIL | FAIL | blocked |

**Summary: V=0 / I=3 / R=3 / B=3**

---

## Infrastructure Bug (HIGH): Duplicate `list_actions` in Tool List

### Evidence (primary data)

Trace file: `/tmp/reyn-worktrees/b27-3/traces/recall_fresh.jsonl`

LLM call `f27e3ee0-faf3-40d0-b126-9c2e66cb5245` tools list (13 entries):
```
list_actions, describe_action, invoke_action, list_actions, exec__run,
web__fetch, web__search, file__grep, file__read,
memory.operation__remember_shared, skill__skill_builder,
skill__skill_improver, skill__skill_importer
```

`list_actions` appears at positions 0 AND 3. Gemini rejects this with:
```
"Duplicate function declaration found: list_actions"
```

This error blocked all scenarios that triggered a fresh agent session
AFTER `.reyn/state/action_usage.jsonl` accumulated `list_actions` as a
recorded action.

### Root cause (observed, not speculated)

Primary data: `.reyn/state/action_usage.jsonl` contents:
```
{"qualified_name": "file__read", "ts": ...}
{"qualified_name": "file__grep", "ts": ...}
{"qualified_name": "web__search", "ts": ...}
{"qualified_name": "web__fetch", "ts": ...}
{"qualified_name": "exec__run", "ts": ...}
{"qualified_name": "list_actions", "ts": ...}
```

The `list_actions` universal catalog wrapper was itself recorded in the
action usage tracker during an earlier session turn. `get_top_n()` in
`ActionUsageTracker` returns `list_actions` as a top-ranked action.
`_build_hot_list_aliases()` (router_loop.py:357) then creates a hot-list
alias for `list_actions`. This alias is appended by section K of
`build_tools()` (router_tools.py:902-910), while section I already appended
`list_actions` as a universal catalog wrapper. No dedup exists between
universal wrappers and hot-list aliases.

Source files:
- `src/reyn/tools/action_usage_tracker.py` (get_top_n at line 137)
- `src/reyn/chat/router_loop.py` (hot list construction at lines 563-588)
- `src/reyn/chat/router_tools.py` (section I at 844-866, section K at 892-910)

### Scope

Confirmed blocked on 6 independent fresh agent sessions. The first 4 warm
runs succeeded because action_usage.jsonl did not yet contain `list_actions`.

Workaround: filter universal-wrapper names (list_actions, describe_action,
invoke_action, search_actions) from hot-list candidates in get_top_n or
_build_hot_list_aliases.

---

## Systematic Miss: `skill_run_spawned` never emitted

All 9 scenarios assert `must_emit: [{type: skill_run_spawned, count: ">=1"}]`.

Event logs for S1, S3, S4 (observable reply scenarios) confirm: zero
`skill_run_spawned` events. The chat router dispatches ops directly via
`invoke_action` without spawning a skill run. This is architectural — the
chat router is not a skill executor. The assertion may reflect a mismatch
between scenario design intent and chat router execution model.

---

## Systematic Miss: No workspace artifacts

All 9 scenarios assert `artifacts: [{present: true}]`. `.reyn/artifacts/`
directory is empty after all runs. Chat router returns results inline as
chat replies; it does not write workspace artifacts.

---

## Scenario-Specific Findings

### S1: file_read_via_chat — inconclusive

Reply summarized P1-P8 principles correctly (workspace as single source of
truth, stateless phases, skill-agnostic OS etc.). Rubric PASS. Events:
tool_executed PASS, skill_run_spawned FAIL. Artifacts FAIL.

### S2: file_glob_grep — refuted

Routing error: `file__grep` has no routing rule (`no routing rule for
category 'exec'`). Agent replied it cannot grep. `file__grep` appears in
DEFAULT_HOT_LIST_SEED but is not a registered action. Rubric FAIL.

### S3: web_search_query — inconclusive

`web_search_started` and `web_search_completed` both emitted. Reply
mentions OpenAI SDK v2.30.0 from March 2026. Rubric PASS. skill_run_spawned
FAIL. Artifacts FAIL. Core op demonstrably works.

### S4: web_fetch_url — inconclusive

`web_fetch_started` emitted. Reply is structured Python 3.12 summary with
PEP numbers (695, 701, 684). Both rubric criteria PASS. skill_run_spawned
FAIL. Artifacts FAIL.

### S5: sandboxed_exec_simple — refuted

LLM called `exec__run` (non-existent); routing returned error suggesting
`exec__sandboxed_exec`. LLM did not retry. No sandboxed_exec events. No
output "4". Fresh agent blocked by dup-tool bug independently.

### S6: lint_a_skill — refuted

Session-accumulated run: LLM called list_actions, received skill list, then
asked for confirmation rather than running lint. `index_events` skill not
found in catalog. No lint_completed event. Fresh agent blocked by dup-tool
bug independently.

### S7-S9: blocked

All three blocked by duplicate list_actions infrastructure bug. Zero
recovery attempts succeeded across 3 separate invocations.

---

## Calibration Check (pre-conclusion 5Q)

Applied before the dup-tool infrastructure finding:

1. Specific observations: 6 fresh agent tests, 2 trace inspections (raw
   tool payload), 1 action_usage.jsonl file read. All confirm the duplicate.
2. Primary data: tool list from REYN_LLM_TRACE_DUMP (direct LLM payload),
   action_usage.jsonl contents. Not inference.
3. Falsification: tested without --eager-embedding-build (blocked), tested
   multiple fresh agents (all blocked), checked source code for dedup logic
   (none exists between section I and K of build_tools).
4. Infra adequacy: REYN_LLM_TRACE_DUMP captures exact tools[] sent to LLM.
5. N/N: directly inspected 6/6 fresh agent runs — all blocked. First 4 warm
   runs worked because action_usage.jsonl had not yet recorded list_actions.

---

## Blockers for Future Runs

1. [CRITICAL] Filter universal wrapper names from hot-list candidates.
   Fix: exclude list_actions/describe_action/invoke_action/search_actions
   from ActionUsageTracker.get_top_n() results or from _build_hot_list_aliases.
2. [HIGH] file__grep not registered despite appearing in DEFAULT_HOT_LIST_SEED.
3. [HIGH] exec__run has no routing rule; exec__sandboxed_exec is correct.
   LLM does not auto-retry after routing error with suggestions.
4. [MED] skill_run_spawned assertions incompatible with chat router architecture.
5. [MED] artifacts assertions incompatible with chat router execution model.
