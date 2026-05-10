# S6 retest (Batch 18): Multi-source recall — Finding

**Batch**: 18 (2026-05-10)
**Scenario**: S6 — Multi-source recall (retest after `0014310` build_tools fix)
**N**: 3
**Verdict**: **refuted (3/3)**

---

## TL;DR

The B17-S6-1 fix (`0014310`) is **structurally verified**: `recall` now appears in
the LLM tool catalog in all 3 runs (`catalog_has_recall: True`). However, on
the prompt "How is recall implemented?", the LLM **does not choose `recall`** —
it picks `reyn_src_read(path="README.md")` instead, then text-replies in turn 2.
0/3 multi-source verifications. New LLM-behavior attractor surfaced
(R-RAG-srcread) for explanatory prompts about Reyn internals.

Multi-source rate measured: **0/3**.

---

## Setup

Per-shot setup (workspace `/tmp/reyn_s6_b18_ws`, fresh history each run, N=3):

1. Reset workspace; `reyn agent new default`.
2. `register_fake_embedding_provider()` via `sitecustomize.py` on `PYTHONPATH`
   (so the chat subprocess can resolve `provider="fake"`).
3. Seed 2 sources via `write_index_directly()`:
   - `reyn_docs` — 6 chunks of recall/index/embedding docs.
   - `reyn_src` — 6 chunks of `handle_recall` / `handle_embed` source.
4. `reyn source list` confirmed both sources, 6 chunks each, in all runs.
5. Subprocess env: `REYN_EMBEDDING_PROVIDER=fake`,
   `REYN_LLM_TRACE_DUMP=/tmp/reyn_s6_b18/run_<i>.jsonl`,
   `LITELLM_API_BASE` via copied `reyn.local.yaml`, `OPENAI_API_KEY` from shell.
6. `reyn chat --cui` stdin `"How is recall implemented?\n"`, timeout 180s.

Driver: `scripts/dogfood_s6_b18_driver.py` (committed in this batch).

---

## Per-run results

| Run | rc | catalog_has_recall | recall_called | turn 1 tool      | turn 2 reply (head) | verdict |
|-----|----|--------------------|---------------|------------------|---------------------|---------|
| 1   | 0  | True               | False         | `reyn_src_read(path="README.md")` | "Recall is implemented as a search over indexed sources..." | refuted |
| 2   | 0  | True               | False         | `reyn_src_read(path="README.md")` | "Recall, in Reyn, is implemented through an indexed source system..." | refuted |
| 3   | 0  | True               | False         | `reyn_src_read(path="README.md")` | "Reyn implements recall using a tool called `recall`..." | refuted |

System prompt always contained the indexed-sources block (verified):

```
## Indexed sources (2 available)
- **reyn_docs** — ... (6 chunks)
- **reyn_src** — ... (6 chunks)
Use the `recall` tool with `sources=[<name>, ...]` to search.
```

Tool catalog count: 17 in all runs. Both `recall` and `drop_source` present.

---

## Verdict breakdown

| verdict | count | rate |
|---------|-------|------|
| verified | 0 | 0% |
| refuted | 3 | 100% |
| inconclusive | 0 | 0% |
| blocked | 0 | 0% |

Multi-source verified rate: **0/3 = 0%** (predicted 70%).

---

## What happened

- **Structural fix verified**: `recall` is now in `tools=` array (B17-S6-1
  closed). Compare batch 17 where catalog excluded both `recall` and
  `drop_source` (5/5 blocked, empty-stop).
- **New LLM attractor R-RAG-srcread**: when the prompt asks "How is X
  implemented?" about Reyn itself, the LLM strongly prefers `reyn_src_read`
  (= read the project's actual source via the source-tree tool) over `recall`
  over indexed sources. README.md was chosen identically in all 3 shots.
- **Two-turn pattern**: After `reyn_src_read` returns README content, the LLM
  text-replies with a generic answer about `recall` rather than continuing to
  invoke `recall` with both sources. The model treats the indexed sources
  block as descriptive ("we have docs"), not prescriptive ("use both").

The B17 prediction R-RAG5 (= LLM picks 1 source of 2) didn't materialise
because the LLM didn't call `recall` at all — a different (stronger) attractor
diverted the entire path.

---

## Calibration delta

| Outcome | predicted | actual |
|---------|-----------|--------|
| verified | 70% | 0 |
| refuted | 25% | 100% |
| inconclusive | 5% | 0 |
| blocked | 0% | 0 |

Brier = ((0.70−0)² + (0.25−1)² + (0.05−0)² + 0² ) / 4
      = (0.49 + 0.5625 + 0.0025 + 0) / 4 = **0.2638**

Prediction missed direction: assumed structural fix would yield ~70% recall
invocation, but the actual blocker is now a **prompt-vs-tool-affordance
mismatch** (= LLM reads README via reyn_src_read instead of issuing a
semantic query). Refuted-dominant outcome was within the predicted refuted
share but for a wholly different reason than R-RAG5. Original principle 8
(observe-before-speculate-llm) reaffirmed: tool-availability ≠ tool-choice.

---

## Carry-over

- **R-RAG-srcread** (new, MED): for "how is X implemented?" prompts about
  Reyn internals, LLM prefers `reyn_src_read` over `recall`. Candidate
  mitigations (deferred to next wave):
  1. Tighten `recall` description — emphasise "use this for indexed
     documentation, not source-tree exploration".
  2. Order tool catalog so `recall` precedes `reyn_src_read` when indexed
     sources are present.
  3. SP rewording: "Prefer `recall` over `reyn_src_read` when the question
     concerns concepts present in indexed sources."
- **R-RAG5** unmeasured: multi-source-vs-single-source preference still
  unknown — needs a prompt that forces `recall` invocation (e.g. "Search the
  indexed sources for ..." or pre-disable `reyn_src_read`).
- **B17-S6-1 fix structurally verified**: catalog wiring + dispatch path are
  both correct; the regression vs batch 17 is from blocked (infra) → refuted
  (LLM attractor), which is the expected post-fix regime.
- Trace artifacts: `/tmp/reyn_s6_b18/run_{1,2,3}.jsonl`,
  `/tmp/s6_b18_results.json`.
