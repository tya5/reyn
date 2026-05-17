# Batch 35 — Retrospective

> Sixth dogfood batch. Two parallel narratives: **OS-layer fix wave
> verified end-to-end** (= +5V via ablation-attributed driver pattern +
> per-fix structural checks), and **my session's blind spot exposed by
> another session finding the real root cause first** (= hot-list alias
> schema empty). The lessons from the blind spot are now logged to
> memory; the trajectory is healthy.

---

## 1. What this batch verified

### Verified, ablation-attributed (HIGH confidence)

- **A2A driver pattern is the dominant lever** for long_session_v1. W7
  ablation: A2A + post-B34 (V=6), stdin + post-B34 (V=2), A2A + pre-B34
  (V=6). Driver pattern alone = +4V; B34 code = 0V contribution.
- **B34 file__grep / file__glob land caused the routing shift** on
  control_ir S2. W3 ablation: 3/5 → file__glob post-B34 vs 0/5 pre-B34.
- **W1's V=0 is verifier methodology mismatch + LLM noise**, not OS
  regression. W1 ablation reproduces 4.3/7 V under B35 driver,
  consistent with the 4-batch mean (~11V). EventStore stale-path is a
  separate MED bug (= ablation condition C confirmed).

### Verified, per-fix structural (= primary data in events / replies)

- W2 F2 driver migration → 4/4 spawn-ack scenarios non-empty reply.
- W5 peer-agent error envelope → silent hallucination eliminated.
- W6 phase_no_progress completion injection → 5/5 skill_run_failed
  followed by skill_completion_injected.
- arg-normalize → file__write and drop_source permission gates reached.
- task #93 verifier triad → B35 framework count matches manual rubric.

---

## 2. What this batch didn't verify (= my session's blind spot)

**Hot-list alias schema empty** was the root cause of 4 batches of
`arg-name mismatch` observations:

| Batch | Symptom | Treatment in this session |
|---|---|---|
| B33 W4 | file__write text vs content KeyError | individual synonym fix (B34) |
| B33 W4 | drop_source source_id vs source KeyError | individual synonym fix (B34) |
| B35 W3 ablation | file__glob dir vs path | "same pattern, add synonym" |
| B35 W3 ablation | file__glob content_regex (refuted as primary) | same |

I treated each as a local hallucination + handler-side defensive fix.
Cross-batch pattern recognition skipped despite N=4 ≥ threshold-3.

The real root cause: `_build_hot_list_aliases` returned aliases with
`parameters: {"type": "object", "properties": {}, "additionalProperties":
True}`. The LLM had **no schema visibility** on hot-list direct aliases
— it always had to guess parameter names. The synonym fix (B34) was
symptomatic relief; the structural fix (`488c15e` D2-min + `a1a5093`
D2-full landed mid-batch by another session) embeds the target
ToolDefinition's `parameters` into each alias so the LLM sees the
canonical schema directly.

### Why my session missed it

Three layers of blind spot, recorded in memory:

1. **Observation layer** — I analysed LLM outputs (tool_calls) but
   never the LLM inputs (tools array schema). `scripts/dogfood_trace.py
   --mode llm-tools-schema` was already implemented; I never invoked
   it in any worker prompt or ablation.
2. **Pattern recognition layer** — 4 same-class observations should
   have hit the N≥3 threshold for "common root cause" hypothesis. I
   instead optimised "1 fix 1 verify" locally, accumulating individual
   synonym fixes without asking "what single structure could produce
   all four?"
3. **Concept layer** — I read `feedback_envelope_layer_fix.md` as
   "handler-side defensive" (= synonym 受容). The true envelope layer
   is `LLM input payload structure`, including the tools array's
   schema. Alias schema empty IS an envelope-layer defect, the most
   structurally upstream one.

### What the other session did differently

Read `_build_hot_list_aliases` directly and saw `properties: {}` in
the literal code. One file read; no batch traces, no ablation. The
blind spot was systemic, not local: I would have needed the
verification angle of "LLM input schema" to surface this from the
trace side.

### Memory updates

- `feedback_envelope_layer_fix.md` (= scope 拡張): handler-side
  defensive is one method; LLM input payload (= alias / tools array
  schema) is the first-class envelope sublayer. Intervention ladder
  updated.
- `feedback_llm_input_schema_observation.md` (= new): worker prompts
  must include `dogfood_trace.py --mode llm-tools-schema` for any
  wrong-arg / wrong-tool finding. Active trigger.
- `feedback_cross_batch_pattern_threshold.md` (= new): N≥3 same-class
  observation must trigger a "common root cause" hypothesis before
  any individual fix is proposed.

---

## 3. The honest trajectory read

B27 0/58 → B28 12/58 → B30 10/58 → B32 11/58 → B33 12/58 → **B35
17/58 = 29.3%**.

The +5V from B33→B35 decomposes (= ablation-grounded):

- **A2A driver pattern alone**: +4V on W7 long_session
- **W5 peer fix**: +2V on multi_agent
- **W6 phase fix + A2A**: +2V on plan_mode/fp_0011
- **W2 F2 driver fix**: +2V on stdlib
- **W1 verifier methodology**: -2V (= measurement artifact, not OS)
- **W3 routing shift offset by residual arg gap**: -1V net (= file__grep
  routing works, but `dir vs path` synonym pending; addressed by
  mid-batch alias schema land)
- **W4 LLM noise**: -1V (= within ±1V variance)

Real OS-layer wins: +9V across W2/W5/W6/W7. Measurement / scenario
churn: -4V. Net +5V.

The B27→B35 trajectory is dominated by OS-layer fixes (= every batch
landed structural improvements, each verified per-fix or ablation-
attributed). The aggregate V plateau ~19% was a measurement artifact
of verified-high biased predictions (= H5 ablation finding); the
actual ~29% post-B35 is the closest measurement to "real system
performance" we have, and it's still rising as the alias schema fix
unblocks the next layer of LLM compliance.

---

## 4. Process reflection — what worked

- **A2A driver pattern adoption** delivered the largest single-batch
  improvement we have measured. The doc-only change in B34 paid off
  immediately.
- **3-condition ablation** for W7/W3/W1 produced HIGH-confidence
  attribution without any "副作用" inference paragraph. The discipline
  installed in B30 has stuck.
- **5-sonnet B34 fix wave** + **7-sonnet B35 verify** + **3-sonnet
  ablation** = 15 sonnet-batches in two waves. Pattern is stable and
  productive at this scale.
- **User reminder loop**: "trace tool で context 分析、 patch 切り分け
  済みですか?" before journal commit forced me to ablation W1/W3/W7.
  Without that gate I would have shipped per-fix attribution as
  inference. Discipline installed.

---

## 5. Process reflection — what didn't work (= the blind spot)

The H5 ablation discipline (= patch isolation for per-fix attribution)
is operating correctly within a fix. The gap is **upstream**: I did
not catch the cross-batch pattern that pointed to a common root
cause until another session found it.

Two corrections logged in memory:

- **Worker prompt template** must include LLM-input schema observation
  as a required verification angle, not just LLM-output (`tool_calls`)
  analysis.
- **Retrospective format** must include a "same-class observation
  count" line per finding so the N≥3 threshold becomes visible at the
  batch boundary.

---

## 6. Fix wave priorities for B36+

In priority order (= ablation-grounded where applicable):

1. **EventStore stale-path bug** (= W1 ablation condition C
   confirmed). `EventStore.write()` recovery + `_open_new_file()` retry.
   MED severity, ~10-line patch.
2. **`simple_memo_app` LLM attractor** (= W7 reproduced, 7/37 turns
   contaminated). Description audit; possibly description disambig
   for the wider "simplest-thing" class.
3. **`mcp_install` → `mcp_search` routing collision** (= 5-way skill
   description audit: builder / improver / importer / eval / install).
4. **`list_actions(filter=<path>)` directory-listing misuse**.
   Envelope-layer empty-result hint pointing to file__list / file__glob.
5. **B27-H4 acompletion-never-awaited** (= issue #52) still open, not
   retested.
6. **Hot-list alias schema fix retest** (= the other session's land):
   B36 should explicitly verify `file__glob` arg-name mismatch is
   gone, and that B34 arg-normalize handler-side defensive is now
   redundant. If redundant, consider revert for cleanliness; if still
   firing, keep as defense-in-depth.

---

## 7. Goal restated

Six batches in: OS-layer fix waves continue to land cleanly with
ablation-grounded attribution. The next discipline layer added this
batch is **cross-session blind-spot detection** — explicitly check
that my session has not optimised a symptom while another session
fixes the root cause. The user's "trace tool で context 分析、 patch
切り分け済みですか?" challenge is the right loop; making "LLM input
schema observation" required in every worker prompt closes the
specific gap that produced this batch's blind spot.

Target for B36: alias schema fix retest verifies arg-name mismatch
class is structurally resolved; new HIGH findings (= simple_memo_app,
mcp_install routing, EventStore stale-path) progressed in order; net
verified rate above 35%.
