# B8-S2 Option F UX — Empty Stop Observation

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e15019` |
| Verdict | **blocked** |
| Predicted top | verified (40%) / blocked (25%) |

## Setup

Observed within the same single session as S1 (input wording identical).
All 9 LLM payloads from `.reyn/llm_trace_b8s14.jsonl` examined for empty stop.

## Observation

### Empty stop scan results

```
9 LLM response entries examined:
  finish=tool_calls content_len=70  tools=1   → router turn 1 (invoke_skill)
  finish=stop       content_len=295 tools=0   → prepare turn 1
  finish=stop       content_len=392 tools=0   → analyze_skill turn 1
  finish=stop       content_len=392 tools=0   → analyze_skill turn 2
  finish=stop       content_len=392 tools=0   → analyze_skill turn 3
  finish=stop       content_len=566 tools=0   → analyze_skill turn 4 (with eval.md read)
  finish=stop       content_len=381 tools=0   → analyze_skill turn 5 (abort decision)
  finish=stop       content_len=442 tools=0   → prepare abort propagation
  finish=stop       content_len=87  tools=0   → router final reply

Empty stop (finish=stop + content="" + tool_calls=[]):  0 / 9 = 0%
```

### attractor detection

```
$ python scripts/detect_attractor.py --trace .reyn/llm_trace_b8s14.jsonl
Total LLM calls: 9
Detected attractors: 0 (0%)
  (none)
```

### router_empty_response event

```
$ grep -r "router_empty_response" .reyn/
(no output)
```

No `router_empty_response` event was emitted in the WAL or events dir.

### Option F code path

The Option F implementation (ADR-0021) handles the case where the router LLM returns
`finish_reason=stop` with empty content and no tool_calls. This pattern was observed at
~50% frequency in batch 7 (N=10). In this batch 8 session:
- 0 empty stops across 9 LLM calls
- G12 truncation fix (`cdbd853`) was landed before batch 8
- The router call itself (call 1) returned `finish=tool_calls` (direct invoke_skill)

## Verdict reasoning

`blocked`: No empty stop occurred in this run. The Option F code path was not exercised.
The G12 truncation fix appears to have reduced or eliminated empty stop frequency — which
was the predicted `blocked` scenario (scenarios.md: "G12 truncation fix で empty stop rate
が下がった場合、観測機会自体が減って blocked").

This is a positive outcome: the truncation fix may have fixed the root cause of empty stops.
But it means Option F UX (clean failure message for empty stop) cannot be confirmed via this
session. The `router_empty_response` event emission and retry-suppression logic remain
**unverified by e2e observation**.

Prediction was 40% verified, 25% blocked. Actual: blocked. Well within the predicted probability.

## Implications

- G12 truncation fix may be sufficient to eliminate empty stops at the router level — this
  would make Option F effectively dead code (which is a good outcome).
- To confirm Option F works: either (a) force-trigger empty stop via `--patch` replay on a
  pre-fix payload, or (b) observe a future batch where G12 hasn't been applied.
- The router behavior changed significantly vs B7-S1: 1-turn direct invocation vs 5-turn
  exploration. This reduces the surface area for G12 to manifest per session.
- batch 9 recommendation: run `llm_replay.py` with `--patch 'content=""'` on request
  `6bb076f0` to synthetic-trigger empty stop and verify Option F UX without waiting for
  natural occurrence.
