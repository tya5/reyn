# B8-S4 G12 Truncation Fix Effect — Skill Description Length Verification

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e15019` |
| Verdict | **partially verified** (system prompt truncated; tool function descriptions not truncated) |
| Predicted top | verified (70%) / inconclusive (15%) |

## Setup

Observed within the same single session as S1. LLM trace: `.reyn/llm_trace_b8s14.jsonl`.
Router request_id: `6bb076f0-fdcb-4344-a7a2-0d818698d532`.

## Observation

### System prompt skill descriptions (inline `Available skills` section)

Measured from the system prompt text of the router's first LLM call:

```
 76  direct_llm: "Catalogue-gap fallback: hand a single-shot natural-language task straight to"
 83  eval: "Evaluate a target skill against a single test case using judge_phase as LLM-as-j..."
 48  eval_builder: "Auto-generate an eval spec (eval.md) for a skill"
 83  judge_phase: "Evaluate a single phase artifact against quality criteria and return a structure..."
 83  mcp_search: "Search github.com/mcp for MCP servers relevant to a natural-language capability ..."
 64  read_local_files: "Read one or more local project files via a configured filesystem MCP"
 64  skill_builder: "Generate a new skill from a natural-language description"
 80  skill_importer: "Search a public skills registry, let the user pick a candidate, and import"
 83  skill_improver: "Iteratively improve an existing skill by working on a temp copy, running eval, p..."
 64  word_stats_demo: "Demo of the python preprocessor step: a Python function computes"
```

All system prompt descriptions: max 83 chars (= 80 chars + `...` suffix). Minimum 48 chars.
The truncation with `...` is visible for `eval`, `judge_phase`, `mcp_search`, `skill_improver`
(all originally > 80 chars). Descriptions ≤80 chars appear untruncated.

**Confirmed: system prompt `Available skills` section applies `MAX_DESC_LEN_FOR_LISTING = 80`
with `...` suffix.**

In batch 7, `skill_improver` was observed at 218 chars. In batch 8: 83 chars (80 + `...`).
Reduction: 218 → 83 chars (-62%).

### Router tool function descriptions (invoke_skill tools schema)

```
206  list_skills
155  describe_skill
110  list_agents
 97  describe_agent
187  list_memory
113  read_memory_body
349  invoke_skill       ← NOT truncated
 36  delegate_to_agent
140  remember_shared
152  remember_agent
 98  forget_memory
```

The router tool function descriptions (the `function.description` field in the JSON tools schema)
are **not truncated**. `invoke_skill` is 349 chars. These are the OS-level tool descriptions,
not the per-skill descriptions from the catalogue. The truncation fix applies only to the
system prompt's `Available skills` section, not to the tool schema descriptions.

This is **expected behavior**: `MAX_DESC_LEN_FOR_LISTING` applies to the skill catalogue listing,
not to the tool API schema. The tool descriptions are part of the OS interface and remain verbose.

### Empty stop rate

```
0 / 9 LLM calls = 0% empty stop rate
(vs batch 7: ~50% rate from N=10 measurement)
```

The attractor detector found 0 attractors. All calls returned substantive content.

### Pattern A verification

Pattern A was defined as: verbose skill description in router context → empty stop.
In batch 8, the system prompt skill descriptions are truncated. The router made 1 LLM call
and got a successful `tool_calls` response (direct `invoke_skill`). Pattern A did not manifest.

However, caution: this is N=1 session. The batch 7 N=10 measurement showed 50% empty stop rate.
Absence of empty stop in a single session is consistent with both "fix works" and "natural
variance — would have been 50% anyway". Replay experiment needed for statistical confidence.

## Verdict reasoning

`partially verified` (mapped to `inconclusive` for 4-category purposes):

- **Verified**: System prompt `Available skills` descriptions are truncated to ≤83 chars. 
  `skill_improver` specifically went from 218 → 83 chars. This directly confirms the
  G12 truncation fix (`cdbd853`) is active and working on the primary payload path.
- **Inconclusive**: Router tool function descriptions remain verbose (349 chars for `invoke_skill`).
  Whether these contribute to empty stop triggering is unknown — batch 7 root cause attributed
  empty stop to system prompt length, but tool schema was not isolated as a variable.
- **Inconclusive**: 0/9 empty stop rate cannot distinguish "fix eliminated them" from "lucky run".
  Would need N≥10 replay to establish statistical confidence.

Prediction was 70% verified. Actual is between verified and inconclusive. The structural check
(description length) is confirmed, but the behavioral outcome (empty stop rate reduction) is
inconclusive from a single session.

## Implications

- Primary goal of S4 (description ≤80 chars confirmed): **achieved**.
- The router tool function descriptions remain verbose. If these contribute to empty stops,
  a second truncation pass may be needed. Low-priority given 0 empty stops observed.
- Next verification step: run `llm_replay.py` with `--n 10` on the router request to get
  a statistically meaningful empty stop rate under current truncated descriptions, then
  `--patch 'system_prompt.replace(...)=<verbose desc>` to compare with pre-fix baseline.
- The 1-turn router behavior (no list/describe calls before invoke_skill) is a new positive
  observation that further reduces per-turn token count vs the 5-turn pattern in B7-S1.
