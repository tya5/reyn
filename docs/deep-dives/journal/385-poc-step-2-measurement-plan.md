# Issue #385 PoC — Step 2 Measurement Plan

**Status**: Active — Step 1 foundation landed (PR #396), design contract
[frozen 2026-05-22](https://github.com/tya5/reyn/issues/385#issuecomment-4514611036),
B49 dispatch pending sandbox_2.
**Owner**: tui-coder (Step 2 measurement design) + sandbox_2 (dogfood execution).
**Depends on**: PR #396 merged (Step 1 foundation = web_fetch preview + read_tool_result + MediaStore reuse).
**Persists**: [Step 2 design comment](https://github.com/tya5/reyn/issues/385#issuecomment-4510287573) on issue #385.

## Status update — 2026-05-22 post-frozen-contract

Two decisions landed AFTER this doc's initial author pass; both are
recorded here so sandbox_2 (= B49 executor) and any new reader can pick
up the current state from this artifact alone without scrolling
issue #385:

1. **Design contract frozen** (= [e2e-coder comment](https://github.com/tya5/reyn/issues/385#issuecomment-4514611036),
   user `OK で先に進めて` ack):
   path_ref shape locked with `resource_uri = reyn-tool-result://<agent>/<artifact>`,
   Phase 1 persistence (a) `no auto-GC`, `agent_id = agent name` (durable
   identity, not chain_id), 4 LLM-actionable error states (`not_found` /
   `expired` / `hash_mismatch` / `unauthorized`).
2. **Wire scheme refinement in arbitration** (= [e2e-coder comment](https://github.com/tya5/reyn/issues/385#issuecomment-4516036105),
   user direction `現実装関係なく恥ずかしくない、 素直な scheme にして`):
   proposal to replace the vendor `reyn-tool-result://` scheme with a
   plain HTTPS URL (`https://<host>/agents/<agent>/resources/<artifact>`).
   Sub-task 3 (= cross-host RPC) is gated on this arbitration; the
   refinement is **wire-shape only**, not metric-defining.

**Implication for B49 (= this measurement plan)**: NONE on the metric
side. Read-skip-rate / expand-rate / cost-delta / paraphrase-fidelity
all operate on the OP layer (`web_fetch_completed` →
`read_tool_result` decided-or-skipped). The wire shape (`resource_uri`
vs `url` field, vendor scheme vs HTTPS) is consumed only by the
producer / consumer infrastructure underneath, not by the metric
detectors. sandbox_2 can run B49 against the current build without
waiting for the scheme arbitration to resolve.

The Open Questions section near the bottom of this doc is still the
right list to discuss with sandbox_2 during dispatch; nothing in the
above two updates resolves any of those Q1-Q5.

This document is the long-form workspace artifact for Step 2 of the
cluster type 5 path established in [feedback_cluster_type_5_pattern](https://github.com/tya5/reyn/issues/385#issuecomment-4509061835)
(`foundation + measure + expand`). It locks down the metric
definitions, scenario shapes, judge_phase prompt templates, and
cofounder controls so sandbox_2 can iterate on a stable target during
B49+ dispatches.

## TL;DR

PoC PR #396 transitions ``web_fetch`` from inline-content return to
``preview + path-ref`` + ``read_tool_result(path)`` companion. Step 2
quantifies whether the LLM actually leverages the preview-first design
(= cost win + 改変 noise win) or wastefully expands every turn (= the
design fails its hypothesis).

Four metrics, ~5 scenarios × N=10 per metric, ~$0.10-0.15 per N=10
cycle for the judge layer. Step 3 decision rule (= **updated
2026-05-22**, see ``Step 3 scope narrowing`` section below):

- ``read_skip_rate > 50% AND cost_delta saves > 20% AND
  paraphrase_fidelity_score ≥ baseline`` → **keep + improve**
  the web_fetch preview-driven design (= prompt tuning, preview
  generator polish, TUI inline-fold UX wave); also unlock A2A
  path-ref materialise design (= cross-consumer reachability)
- otherwise → preview quality reinvest OR pivot to (E)
  markdown-template alternative (= issue #393)

**Not on the Step 3 path**: generalising preview-driven design to
``file_read`` / ``grep`` (= confirmed user direction 2026-05-22 per
the 3-criteria check below). See ``Step 3 scope narrowing``.

## Step 3 scope narrowing (= 2026-05-22 user judgment)

Preview-driven design has unique user-facing value only when all
three of the following criteria are met (= see
``feedback_preview_driven_3_criteria`` memory):

1. **content is inherently external** — user / other UI surfaces
   (A2A / MCP / CLI / Browser) lack independent access. = user
   can ONLY see this content via the LLM, so paraphrase noise
   is hidden.
2. **size is unpredictable + can be large** — 1 KB to multi-MB.
3. **LLM paraphrase noise is a UX hazard** — number drift,
   subject inversion, over-summarisation, fabrication risks
   on long-form prose content.

Checked against the natural candidate tools:

| tool | (1) external | (2) size unpredictable | (3) paraphrase hazard | verdict |
|---|---|---|---|---|
| ``web_fetch`` | yes (= remote HTTP body) | yes (1 KB - MB) | yes (= HTML prose) | ✓ **PoC target** |
| ``file_read`` | no (= workspace fs; user can ``cat`` directly) | no (= ``offset/limit`` pagination exists) | no (= user has original) | ✗ **out of scope** |
| ``grep`` | no (= local) | no (= structured hit list) | no (= matched line structure) | ✗ **out of scope** |
| ``web_search`` | yes | small / structured already | low (= title+snippet) | n/a (= already preview-shaped) |

**"LLM-only cost reduction" does NOT count as unique value** —
every tool benefits from less context tokens, but justifying
preview-driven architecture complexity requires the
paraphrase-noise mitigation that's exclusive to externally-sourced
content the user can't independently verify.

cross-consumer angle reinforces this: criterion (1) ``inherently
external'' is what makes preview-only viable across A2A / MCP /
CLI / Browser — for tools where the user already has direct
access, the preview-driven layer adds complexity without UX
payoff.

Therefore Step 3 (= "expand" in cluster type 5
``foundation + measure + expand``) is narrowed to:

- **keep the web_fetch implementation as the sole preview-driven
  tool** — improve preview quality, TUI display affordance, etc.
- **unlock A2A path-ref materialise design** (= cross-consumer
  reachability for the web_fetch path-ref — separate follow-up
  issue to scope)
- **do NOT generalise to file_read / grep / other in-workspace
  tools**.

## Hypothesis

Preview-driven tool result keeps raw body OUT of LLM context by
default. Two predicted user-visible wins:

1. **改変 noise → ~0**: LLM cannot paraphrase what it doesn't see.
   The user gets the raw body via the path-ref display, or a
   summary the LLM emits FROM the preview only.
2. **cost (prompt tokens) → minus ~30-70%** for ``web_fetch``-heavy
   turns: the body that previously occupied the LLM context now
   lives on disk. Only ``preview`` (= 200-400 tokens) enters the
   model. ``read_tool_result`` expansion is paid for only when the
   LLM consciously decides preview is insufficient.

Both predictions are MEASURABLE. The PoC succeeds if measured
effect sizes match the hypothesis directionally and with adequate
magnitude to justify cluster type 5 Step 3 expansion to
``file_read`` / ``grep``.

## Metrics

Following sandbox_2's round-1 N-rigor recommendation + lead-coder's
"4 metrics 直交 cross-validate" pattern.

### Metric 1 — read_skip_rate

**Definition**: fraction of web_fetch turns where the LLM answered
the user **without** calling ``read_tool_result(path)``.

```
read_skip_rate = #{turns: web_fetch fired AND read_tool_result NOT fired before agent_reply}
                 / #{turns: web_fetch fired}
```

**Type**: binary per-turn.
**N**: 10 per scenario (= ±10% confidence band, sandbox_2 r1 Q2).
**Source**: dogfood trace dump events. Look for ``web_fetch_completed``
followed by ``agent_message_sent`` without an intervening ``tool_called``
(action=``read_tool_result``).

**Hypothesis target**: > 50% (= at least half of fetches resolve
from preview only).

### Metric 2 — expand_rate

**Definition**: fraction of web_fetch turns where the LLM called
``read_tool_result(path)`` at least once before the agent reply.

```
expand_rate = #{turns: web_fetch fired AND read_tool_result fired}
              / #{turns: web_fetch fired}
```

**Type**: binary per-turn (sibling of read_skip_rate; together ≈ 1.0).
**N**: 10.
**Source**: same trace dump events as Metric 1.

**Hypothesis target**: 20-50% — high enough that the LLM can answer
nuanced questions, low enough that the cost win materialises.

### Metric 3 — cost_delta_tokens / cost_delta_usd

**Definition**: difference in cumulative prompt+reply token / cost
between baseline (= pre-PR-#396 reyn build) and post-PoC build for
the same scenario set.

```
cost_delta_tokens = sum(prompt_tokens + completion_tokens) over scenario set [POST]
                    - sum(prompt_tokens + completion_tokens) over same set [BASELINE]

cost_delta_usd    = similar for usd
```

**Type**: continuous (per-scenario-set aggregate).
**N**: 3-5 per scenario (sandbox_2 r1 Q2; outlier mitigation
sufficient at lower N for continuous metric).
**Source**: budget_tracker events / ``llm_called`` + ``llm_response_received``
events with usage payload.

**Hypothesis target**: ≥ 20% reduction.

### Metric 4 — paraphrase_fidelity_score

**Definition**: severity-weighted aggregate of "LLM reply deviated
from source content" deviations, measured per turn via judge_phase.

```
For each turn in scenario set:
  deviations = stage_B_judge(llm_reply, source_tool_body)
              = [{type, severity 1..5, excerpt}, ...]
  per_turn_score = sum(d.severity for d in deviations)

paraphrase_fidelity_score = mean(per_turn_score over N runs)
```

**Type**: continuous, per-turn.
**N**: 10 per scenario, judge fires only on turns where Stage A
flags suspicious (= screening filter).

**Source**: judge_phase (2-stage hierarchy, see below).

**Hypothesis target**: ≤ baseline (= no worse than pre-PoC). If
the LLM is answering FROM preview (= correctly), it should have
LESS opportunity to fabricate body content.

## Scenarios pool

5 scenarios × N=10 = 50 turns at the binary-metric layer.

### Scenario A (流用 B47 W3 S3) — web_search_query

User prompt: ``Search the web for "Rust async runtime tokio" and
summarise the top result in one paragraph.``

Expected LLM trajectory:
1. web_search → list of URLs + titles
2. (optional) web_fetch on top URL → preview returned
3. Agent reply summarising

**Preview-sufficiency hypothesis**: HIGH (= one-paragraph summary
fits the preview's first_paragraph + outline fields).

### Scenario B (流用 B47 W3 S4) — web_fetch_url

User prompt: ``Fetch https://example.com and tell me what's there.``

Expected trajectory:
1. web_fetch → preview returned
2. Agent reply describing the page

**Preview-sufficiency hypothesis**: HIGH (= "what's there" is
exactly what the preview shows).

### Scenario C (新) — section-specific question

User prompt: ``Fetch the Python documentation page at
https://docs.python.org/3/library/asyncio.html and explain how
asyncio.Lock works specifically.``

Expected trajectory:
1. web_fetch → preview returned with module outline
2. LLM decides preview lacks Lock specifics → read_tool_result(path)
3. LLM searches the body for Lock section
4. Agent reply quoting Lock semantics

**Preview-sufficiency hypothesis**: LOW (= preview lists structure
but not implementation details).

### Scenario D (新) — multi-fetch with reasoning

User prompt: ``Fetch https://example.com and https://www.iana.org/
and tell me which one is more recently updated.``

Expected trajectory:
1. web_fetch URL 1 → preview
2. web_fetch URL 2 → preview
3. LLM compares "last modified" / "updated" from preview metadata
   (= if preview captures it) OR calls read_tool_result on both
4. Agent reply with comparison

**Preview-sufficiency hypothesis**: MEDIUM (= depends on whether
preview captures metadata; this scenario also tests preview
generator quality).

### Scenario E (新) — fetch + structured extraction

User prompt: ``Fetch https://api.github.com/repos/python/cpython
and tell me the star count and last update timestamp.``

Expected trajectory:
1. web_fetch → JSON preview (= top-level keys + first record)
2. LLM extracts star_count + updated_at from preview (= if shown)
   OR calls read_tool_result to load full JSON
3. Agent reply with numbers

**Preview-sufficiency hypothesis**: depends on JSON preview shape
— this scenario also tests Step 2 generalisation feasibility
(= preview generator currently HTML-tuned; JSON variant is a
nice-to-have for Step 3 file_read / grep generalisation).

## Judge_phase (paraphrase_fidelity_score)

Following lead-coder Q5 r2 architecture: **2-stage hierarchy** to
control cost while preserving signal.

### Stage A — cheap screening (flash-lite, every turn)

**Model**: ``openai/gemini-2.5-flash-lite``.
**Cost**: ~$0.001 per turn × 50 turns = ~$0.05 per N=10 cycle.
**Prompt template** (= will land in ``src/reyn/<location>``):

```
You are auditing an LLM agent reply for fidelity to its source.

Source body (raw tool result):
"""
{{tool_result_body}}
"""

LLM reply to the user:
"""
{{agent_reply}}
"""

Question: Is the LLM reply faithfully grounded in the source body,
or does it appear to paraphrase / synthesise / fabricate content?

Output JSON:
{
  "grounded": true | false,
  "suspicious_segments": [ "<one-line excerpt from agent_reply that
   may have deviated, OR empty if grounded=true>", ... ]
}
```

**Decision**: ``grounded=true`` → skip Stage B (= "high-trust" turn).
``grounded=false`` → enqueue Stage B with the suspicious_segments
list as targeted input.

### Stage B — deep judge (strong tier, suspicious turns only)

**Model**: ``openai/gemini-2.5-flash`` (= strong tier per
``reyn.local.yaml``).
**Cost**: ~$0.005 per call × expected 10-30% of stages = 5-15 calls
× $0.005 = $0.025-0.075 per N=10 cycle.
**Prompt template**:

```
You are a strict auditor checking whether an LLM agent reply
deviated from its source tool result.

Source body:
"""
{{tool_result_body}}
"""

LLM reply:
"""
{{agent_reply}}
"""

Stage-A flagged these segments as suspicious:
{{suspicious_segments_list}}

For each deviation you find, classify by type:
- 数値違い: a number / quantity in the reply differs from source
- 主体逆転: subject / object swapped (= who did what to whom)
- 過度な要約: source detail lost in compression
- 捏造: content in reply with no basis in source
- omission_significant: source content was important and omitted

Output JSON:
{
  "deviations": [
    {
      "type": "数値違い" | "主体逆転" | "過度な要約" | "捏造" | "omission_significant",
      "severity": 1 | 2 | 3 | 4 | 5,
      "excerpt": "<reply excerpt that deviated>",
      "source_basis": "<what the source actually said, if any>"
    }
  ],
  "overall_fidelity": 0..100,
  "reasoning": "<one short paragraph>"
}
```

**paraphrase_fidelity_score** per turn = ``sum(d.severity for d in
deviations)``. Aggregate across N runs by mean for the metric value.

### Budget summary

| Component | Per-N=10-cycle cost | Notes |
|---|---|---|
| Stage A screening | $0.05 | 50 calls × flash-lite |
| Stage B deep judge | $0.025-0.075 | 5-15 calls × flash |
| **Total per cycle** | **$0.075-0.125** | acceptable per e2e-coder r2 |

5 scenarios × N=10 = single batch ≈ $0.075-0.125. Re-runs for
baseline vs PoC comparison double this. Total measurement
investment ~$0.20-0.30 for the full PoC effect-size report.

## Cofounder controls

Two warnings from sandbox_2 round 2, both folded into the design.

### Cofounder (a) — preview determinism

**Status**: ✅ resolved by PR #396 design.

The preview generator (``_HtmlPreviewParser``) is a pure function
of the raw HTML. ``test_html_preview_is_deterministic_for_same_input``
pins this contract. Across N runs the preview content is
byte-stable; only the FILENAME varies (= timestamp token).
Measurement variance from preview drift = 0.

**No additional fixture cache needed**. ``dogfood_variant_replay.py``
continues to drive the LLM through fixtures; preview generation
runs deterministically inside the tool side.

### Cofounder (b) — minimal SP baseline

**Status**: ✅ partially landed (= PR #396 web_fetch description is
purely descriptive), variant comparison deferred.

PR #396 keeps the web_fetch tool description purely descriptive
(= no "use preview when sufficient" guidance text). This IS the
minimal SP baseline.

**Variant comparison** (= compare baseline vs "guidance added" SP
to separate LLM-decided vs prompt-engineered behaviour) is a
follow-up wave: same scenario set + N=10 with a second SP variant
that adds explicit guidance. Run AFTER baseline measurement so the
effect size is comparable.

### Cofounder (c) — judge LLM bias

**Status**: addressed by 2-stage hierarchy + cross-validation.

sandbox_2 r2 warning: judge LLMs have "good summary" bias (= judges
prefer natural-sounding summaries) and the model selection itself is
a cofounder. Mitigation:

- Stage A = flash-lite, Stage B = flash → different model classes,
  per-model bias visible if observable
- Cross-validate against the structural N=10 binary metrics: if
  judge says "high fidelity" but read_skip_rate is anomalously low,
  that's a contradiction worth investigating (= judge may be
  scoring overly generously)
- Document any per-judge bias observation in the B49 retrospective
  (= sandbox_2 methodology trail)

## Sequencing & ownership

```
Step 2.0 (= now): tui-coder publishes this doc + draft is on issue #385
Step 2.1: sandbox_2 dogfood resume + B49 dispatch with preview-aware patches
  ↓
Step 2.2: 5 scenarios × N=10 binary runs (baseline + PoC), capture trace dumps
  ↓
Step 2.3: judge_phase Stage A + Stage B (run as separate job from variant_replay)
  ↓
Step 2.4: aggregate metrics, draft B49 retrospective with effect-size report
  ↓
Step 2.5 (Step 3 trigger):
  read_skip_rate > 50% AND cost_delta saves > 20% AND fidelity ≥ baseline
    → keep + improve (= web_fetch preview generator polish,
      TUI inline-fold UX wave, A2A path-ref materialise follow-up)
  otherwise
    → preview quality review OR pivot to issue #393 (E)

Note: Step 3 does NOT include generalising to file_read / grep
(= see ``Step 3 scope narrowing`` above; user direction
2026-05-22 per 3-criteria check).
```

## Open questions for sandbox_2 review

Same 5 questions as issue #385 comment 4510287573, restated here for
self-containment:

1. **Scenarios pool 妥当性**: B47 W3 流用 2 + 新 3 で web_fetch
   多様性十分? scenarios C/D/E は new — sandbox_2 perspective で
   risky / missing axis ある?
2. **Stage A LLM choice**: flash-lite で 「grounded か」 screening は
   precision 出るか? sandbox_2 過去 measurement で flash-lite judge
   の bias 観察した経験 (= "good summary" bias confirm) ある?
3. **expand_rate と read_skip_rate の重複**: 厳密に
   ``expand_rate = 1 - read_skip_rate`` (= 1 turn = 1 web_fetch +
   decide expand) でいい? multi web_fetch per turn の集計どうする?
4. **B49 dispatch 時の dogfood scripts coordinate**: PR #396 merge
   後の ``dogfood_variant_replay.py`` trace dump 形状 (= path_ref +
   preview field) を preview-aware に handle する patch がどこで
   land? sandbox_2 側 (= dogfood scripts owner) で吸収、 それとも
   tui-coder 側 helper 追加?
5. **Minimal SP vs guidance variant 比較 timing**: Step 2 内同 batch
   で 2 variant 動かす vs Step 2 完了後 Step 2b で別 batch? cofounder
   warning (b) として scope に組み込む?

## Tracking

- Issue #385 (PoC tracker)
- PR #396 (Step 1 foundation, landed)
- PR-D (= "Step 2 measurement infra", future PR — judge_phase Python
  code, scenario yamls, aggregation script)
- B49+ dogfood batches will reference back here in their retrospectives.
