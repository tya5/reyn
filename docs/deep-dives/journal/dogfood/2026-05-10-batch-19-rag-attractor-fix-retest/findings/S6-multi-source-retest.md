# S6 retest (Batch 19): Multi-source recall — R-RAG-srcread guidance fix

**Batch**: 19 (2026-05-10)
**Scenario**: S6 — Multi-source recall (retest after `ef70aef` R-RAG-srcread guidance)
**HEAD**: `ef70aef`
**N**: 3
**Verdict**: **refuted (3/3)**

---

## TL;DR

The R-RAG-srcread prompt-layer fix (= `router_system_prompt.py` "for 'how is X
implemented?' prefer recall over reyn_src_read" guidance) is **structurally
present in the system prompt** (verified at offset ~4150 in all runs) but
**fails to redirect LLM behavior**: 0/3 multi-source verifications, identical
attractor recurrence to batch 18. The LLM still picks
`reyn_src_read(path="README.md")` 3/3, then text-replies. The prompt-level
intervention does not bend the gemini-flash-lite affordance bias for
"how is X implemented?" → file read.

Multi-source rate measured: **0/3** (vs predicted 50%).

---

## Summary table

| Axis | Predicted | Actual |
|---|---|---|
| Structural pre-check (recall in catalog + new guidance in SP) | ✓ | ✓ (3/3) |
| Behavioral (recall actually invoked) | 50% | 0% (0/3) |
| Verdict: verified | 50% | 0% |
| Verdict: refuted | 40% | 100% |
| Verdict: inconclusive | 5% | 0% |
| Verdict: blocked | 5% | 0% |

---

## Per-run details

| Run | tool called | sources field | reply (head) | verdict |
|-----|-------------|---------------|--------------|---------|
| 1 | `reyn_src_read(path="README.md")` | n/a | "Reyn's recall functionality is implemented through indexed sources..." | refuted |
| 2 | `reyn_src_read(path="README.md")` | n/a | "Reyn's recall functionality is implemented using indexed sources..." | refuted |
| 3 | `reyn_src_read(path="README.md")` | n/a | "Recall, in the context of this agent, is implemented using indexed sources..." | refuted |

All 3 runs: rc=0, elapsed ~4–5s, catalog_has_recall=True, srcread guidance
text confirmed present in system prompt offset ~4150.

---

## What happened

**Prompt-layer intervention alone is insufficient against R-RAG-srcread.** The
new guidance is delivered to the LLM as designed (system-prompt diff
verified) yet **0/3** runs reroute to `recall`. The attractor pattern is
**100% identical to batch 18**: turn 1 = `reyn_src_read(README.md)`, turn 2 =
generic text reply about recall semantics. README.md is the universal default
target — the LLM never even considers querying via `recall` despite the
explicit "for semantic explanations, recall wins" sentence and the indexed
sources block listing both `reyn_docs` and `reyn_src`.

**Comparison to S5 / S9 fix outcomes**: S5 (`recall vocab disambiguation` +
empty-state hint) succeeded in batch 18 because the fix targeted vocabulary
that the LLM was actively misreading (= memory vs recall confusion was a
**lexical** trap, fixable at prompt layer). S6's R-RAG-srcread is a
**tool-affordance attractor**: the LLM "feels" reyn_src_read is more direct
because file ops have stronger procedural priors in flash-lite's training
distribution than indexed semantic search. Text-anchored guidance does not
override this prior — the model reads the rule, parses it, and then ignores
it under the affordance bias. This matches the meta-feedback from
`feedback_envelope_layer_fix.md` (envelope > schema > SP content for
protocol-level LLM attractors).

---

## Calibration delta

| Outcome | predicted | actual | gap |
|---------|-----------|--------|-----|
| verified | 50% | 0% | -50pp |
| refuted | 40% | 100% | +60pp |
| inconclusive | 5% | 0% | -5pp |
| blocked | 5% | 0% | -5pp |

Brier = ((0.50−0)² + (0.40−1)² + (0.05−0)² + (0.05−0)²) / 4
      = (0.25 + 0.36 + 0.0025 + 0.0025) / 4 = **0.1538**

(vs batch 18 S6 Brier 0.2638 — improvement reflects more accurate prior, not
fix success.)

**Two-axis breakdown (原則 11)**:
- Structural axis: predicted ✓ → actual ✓ (= 100% match, fix lands in SP)
- Behavioral axis: predicted 50% → actual 0% (= prompt-layer fix fully refuted
  for this attractor class)

The prediction was **directionally wrong on behavioral axis**: assumed
explicit anti-attractor guidance would yield ~50% compliance based on
batch 6-12 base rate (60-80% for anti-attractor rules). R-RAG-srcread is a
**stronger** attractor class than the prior rules — likely because
"reyn_src_read on README.md" is a **fallback default** (= empty-prior choice),
not a reasoned choice the rule can override. Adding rule text to
counter-argue against a default produces no observable shift.

---

## Carry-over

- **R-RAG-srcread persists at 100% (3/3) post-prompt-fix**. Prompt-layer
  intervention exhausted. Next-wave candidate mitigations (escalating cost):
  1. **Tool-catalog ordering**: place `recall` before `reyn_src_read` when
     indexed sources exist; tool ordering has empirical effect on flash-lite
     selection in batch 6-9 SP-bloat experiments.
  2. **Tool description tightening**: rewrite `reyn_src_read.description` to
     include "do NOT use for conceptual 'how does X work' questions —
     prefer `recall` when indexed sources match topic". This is **schema-layer**
     not SP-layer (= one rung up the envelope-fix priority ladder).
  3. **Conditional tool removal**: when an indexed source matches the prompt's
     topic semantically (= pre-routing heuristic), suppress `reyn_src_read` /
     `file_read` from the tool catalog for that turn. Strong intervention,
     P7-clean if implemented as a pre-call structural filter.
  4. **Strong-model fallback**: phase 2 G4 spike (= gemini-2.5-flash) likely
     resolves this since the attractor is rooted in the weak-model affordance
     bias.
- **Trajectory ✓ judgement for batch 19**: S6 contributes 0% to verified
  rate. Batch 19 mean depends on S9 outcome alone for milestone parity.
- Trace artifacts: `/tmp/reyn_s6_b19/run_{1,2,3}.jsonl`,
  `/tmp/s6_b19_results.json`.
- **New principle candidate**: "prompt-layer fixes are **bounded** for
  affordance-bias attractors (= where the wrong tool is the LLM's empty-prior
  default). Schema/envelope/model-layer intervention is required." (= refines
  envelope-layer fix principle with a typology of attractor classes.)
