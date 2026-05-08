# Batch 5 Fix-Verify: Prelude

## Context

- **Main HEAD**: `30fdc33` (test(router): rekey LLMReplay fixtures after prompt consolidation)
- **Fixes verified**: B4-H1 (`ffc9b4a`) + B4-H2 (`d9787cb`) + prompt consolidation (`e90c0f2`)
- **Date**: 2026-05-04
- **Model**: gemini-2.5-flash-lite via LiteLLM proxy at localhost:4000

## Applied Fixes

| Commit | Fix | Description |
|--------|-----|-------------|
| `ffc9b4a` | B4-H1 | Route `_run_skill_awaitable` narrator reply to `RouterLoop agent_replies` |
| `d9787cb` | B4-H2 + B4-L1 | Expand `copy_to_work` act budget 3→6 + scope glob to target |
| `e90c0f2` | prompt consolidation | Consolidate router behaviour rules (bloat from B2-H1 + B3-H1 chain) |
| `30fdc33` | test fix | Rekey LLMReplay fixtures after prompt consolidation |

## Scenario Setup

- **Scenario A**: `specialist` agent created (`reyn agent new specialist`); default topology auto-includes both agents
- **Scenario B**: Clean `.reyn/`; default agent only; `direct_llm` as target
- **Tool**: `python3 scripts/dogfood_trace.py --root .reyn --mode summary|chain|cost`
- **Execution method**: piped stdin to `reyn chat default --cui --no-restore`
