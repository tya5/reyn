# S6 Multi-Source Recall Retest (Batch 20)

> Synthetic-source redesign run on `12832f2`. Driver:
> `scripts/dogfood_s6_b20_driver.py`. Trace dumps:
> `/tmp/reyn_s6_b20/run_{1,2,3}.jsonl`. N=3.

## 1. Summary table

| Axis | Predicted | Actual |
|---|---|---|
| Structural: recall in catalog | ✓ | ✓ (3/3) |
| Structural: 2 sources visible in `## Indexed sources` | ✓ | ✓ (3/3) — both `quantum_concepts` and `quantum_code` listed |
| Behavioral: recall invoked | ~95% | 100% (3/3) |
| Behavioral: multi-source picks | 30–70% (= measurement target) | **0% (0/3)** |
| **Verdict: verified** | 40% | **0/3** |
| Verdict: refuted Class B-A1 (1 source only) | ~55% | **3/3** |
| Verdict: refuted Class B-A2 (recall not invoked) | ~5% | 0/3 |

## 2. Per-run details

| Run | Tool calls (in order) | `recall.sources` | Turn-2 reply | Verdict |
|---|---|---|---|---|
| 1 | `recall` × 1 | `["quantum_concepts"]` | concept-level answer (handshake / decoherence buffer / message bus); 0 follow-up tool calls; `finish_reason=stop` | refuted_b_a1 |
| 2 | `recall` × 1 | `["quantum_concepts"]` | near-identical concept-level answer; 0 follow-ups | refuted_b_a1 |
| 3 | `recall` × 1 | `["quantum_concepts"]` | concept-level answer naming `entangler` / `decoherence_buffer` (component **names** from concept doc, not their code); 0 follow-ups | refuted_b_a1 |

All three runs: turn 1 picks `quantum_concepts` only, recall returns chunks from `docs/quantum_bridge.md`, LLM writes a clean prose answer in turn 2 and stops. No run ever issued a second `recall` for `quantum_code`.

## 3. What happened

The structural axis is clean: both sources are visible in the system prompt's
`## Indexed sources` section, the `recall` tool is in the catalog with the
identical description (`"Search indexed sources by natural-language query.
Pick sources from the 'Indexed sources' section in the system prompt."`),
and `reyn_src_read` is no longer being preferred. Affordance conflict was
successfully eliminated — the LLM never reached for `reyn_src_read` or
`web_search`.

What the LLM did instead: it read the two source descriptions, decided the
question "How does the quantum bridge protocol work?" was a conceptual
question, and picked `quantum_concepts` only. Turn-2 it received chunks
about handshake / decoherence buffer / message bus, judged that sufficient,
and emitted a confident prose answer with `finish_reason=stop` and zero
follow-up tool calls. There is no sign of confusion, error, or
self-doubt — the LLM "satisfied" itself with partial coverage.

Run 3 is especially telling: the answer mentions `entangler` and
`decoherence_buffer` by name (because they appear in the concept chunks),
but never quotes the actual `class Entangler:` / `class DecoherenceBuffer:`
implementations from `quantum_code`. The LLM had no felt need to look at
code to answer a "how does it work?" question — and from a user perspective
the answer is genuinely useful.

## 4. Affordance-bias hypothesis verdict

**Pending — partially supported, but the test is confounded by prompt phrasing.**

What we observed (3/3 = 1 source only) is consistent with the
affordance-bias / "1 source satisfies" attractor hypothesis. But there is
a competing explanation that the prelude design did not control for: the
prompt **"How does X work?"** is itself a conceptual-leaning question.
A reasonable LLM (or human) reading two source descriptions —
"conceptual documentation" vs. "source code implementation" — would pick
concepts first and only reach for code if the concept answer felt
incomplete. The concept doc was rich enough that this never triggered.

So this batch demonstrates one of two things, and the data alone cannot
distinguish them:

- (a) **Behavioral attractor**: weak LLM defaults to 1 source even when
  the question warrants multi-source synthesis.
- (b) **Rational routing**: for a "how does X work?" prompt, 1 source
  (the concept doc) genuinely is the right routing — the prompt itself
  doesn't require multi-source.

To break the tie, the next iteration needs a prompt that **structurally
requires** material from both sources (e.g., "Compare the conceptual
description of the decoherence buffer with how it is actually
implemented in code", or two distinct sub-questions joined by "and").
If a prompt that obviously requires both sources still elicits 1-source
recall, that is direct evidence for (a). If multi-source picks rate
jumps to ≥50% under such a prompt, (b) wins and the affordance-bias
hypothesis can be downgraded.

This is a **scenario-design carry-over**, not a Reyn behavior fix.
Per pre-retrospective discipline, this is reported up-front rather
than papered over with a new attractor name.

## 5. Calibration delta

| Item | Predicted | Actual | Delta |
|---|---|---|---|
| Verified rate | 40% (band 30–70%) | 0% (0/3) | -40 pp; below band lower edge |
| Refuted B-A1 | ~55% | 100% | +45 pp |
| Refuted B-A2 | ~5% | 0% | -5 pp |
| recall invoke rate | ~95% | 100% | +5 pp |

The structural prediction was correct. The behavioral prediction band
30–70% was generous on the high side and too tight on the low side — the
actual outcome (0%) sits below the entire predicted range. With N=3 the
binomial 95% CI on "verified rate" is roughly [0%, 71%], so we cannot
rule out a true rate of 30–50% on noise alone, but the cleanness of the
trace (every run satisfied immediately, no half-tries) suggests the true
rate is closer to 0–15% under this exact prompt.

The miscalibration is **not** in the model's RAG behavior estimate —
it is in **scenario design assumption**: the prelude assumed "How does X
work?" would naturally trigger multi-source synthesis, when in fact a
single rich concept doc dominates that prompt class. Lesson: when
predicting multi-source behavior, the prompt must be designed so a
single source is provably insufficient (= one source omits material the
answer needs).

## 6. Carry-over

- **B20-S6-1** (= scenario design): in batch 21+, retest with a prompt
  that structurally requires both sources, e.g.
  - `"Compare the conceptual description of the decoherence buffer with how it is implemented in the code, including specific class / function names."`
  - or `"Walk me through the QBP handshake at the protocol level AND show the actual handshake function code."`
  Drop verdict: keep current Class B-A1 / B-A2 subdivide.
- **B20-S6-2** (= taxonomy): do not yet update memory
  `feedback_attractor_class_taxonomy.md` Class B status to "supported" —
  evidence is consistent with attractor but confounded by prompt phrasing.
  Hold at "pending" until the carry-over scenario runs.
- **B20-S6-3** (= principle): codify "for multi-source recall tests,
  prompt must be designed so single-source answer is provably incomplete"
  as a measurement-design rule. Candidate addition to
  `feedback_pre_retrospective_discipline.md` or a new
  `feedback_multi_source_prompt_design.md`.
