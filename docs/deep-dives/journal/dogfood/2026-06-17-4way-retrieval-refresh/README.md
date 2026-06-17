# 4-way weak-model tool-use-scheme comparison — refresh (2026-06-17)

**Point-in-time dogfood results** (owner request: refresh the comparison, now including
`retrieval`, mechanism-verified by #1604 part-1). Internal-signal only — **pass-rate is
NOT a published leaderboard** (owner standing constraint). Primary value =
structural-defect mining + cross-scheme behavior/efficiency.

## Setup
- HEAD: `b4158974` (post #1616/#1657/#1659/#1693/#1697/#1698; current main at run time).
- Model: weak `gemini-2.5-flash-lite` (+ `text-embedding-3-small`, `embedding_class: standard` for retrieval).
- Schemes: `retrieval` / `enumerate-all` (chat default) / `universal-category` / `codeact` (post-#1659 direct-fn).
- Scenarios (reuse #1631 set, HOST-REPO → no #1667 /testbed foot-gun confound):
  S1 single-read · S2 multi-file-explore · S3 conditional · M1 read-aggregate · M2 read→transform→write chain.
- N=5 per scheme×scenario (100 runs). Per-run 200s cap (timeout = efficiency signal per lead, option-a).
- Harness: `/tmp/refresh_4way.py`; raw results: `/tmp/refresh_4way_results.jsonl`.

## Metrics
- Task-completion (internal-signal), efficiency (round-trips / tool-calls to converge),
  structural-defect markers (reyn_source/run_code misselection [#1667], tool_call-runaway
  degeneracy, malformed act-JSON, codeact-crash, provider-empties, timeouts).

## Results
_(numbers + matrices filled on run completion — see `results.md`)_

## Key findings (filled on completion)
- Fairness control (lead): does the S2/M multi-file SEQUENTIAL-READ round-trip cost appear
  ACROSS all schemes (= weak-model ceiling, don't penalize retrieval) vs retrieval-specific?
- Retrieval multi-step = slow-but-PROGRESSING (sequential reads, 0 search_actions on multi-file)
  = tuning/inefficiency, NOT a structural degeneracy defect (S2 standalone diag: rc=0, 16 round-trips).
- #1667 foot-gun cross-scheme rate on host-repo (expect ~0 → confirms context-specificity).
