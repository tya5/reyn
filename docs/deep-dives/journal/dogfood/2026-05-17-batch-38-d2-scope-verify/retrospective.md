# Batch 38 — Retrospective

> Ninth dogfood batch. **Three B38 fix-wave landings verified
> structurally via primary data**: D2-wrapper scope expansion (= ARS
> covers all session-visible actions, all 7 workers confirmed), R-WEB
> mcp_search routing chain (= 2-batch structural chain B37→B38 with
> e5ecadb gate + 5e05b9b seed), cold-start peer canonical (= B37
> `message` → B38 `request` direct primary data).
>
> The headline is **not** the V=23/58 (= -3V mathematical vs B37). It
> is the **direct application of the G31 lesson published the same day
> in PR #129**: B38's V count drop is mostly **G31 ε2-style description
> dilution + surface fungibility / wrong-path migration**, while the
> structural fixes themselves are verified in events / tool_calls /
> ARS dumps. The verified-rate metric, used as a single indicator
> across 8 batches, was an umbrella that the G31 method directly
> warned about. B38 is the first batch where this critique is applied
> **inside the same retrospective** that documents the result.

---

## 1. What this batch verified — primary data

### Structural fixes landed end-to-end

| Fix | Primary evidence | Worker |
|---|---|---|
| **D2-wrapper scope expansion** (= ARS covers all session-visible actions, not just hot-list) | All 7 workers dumped `invoke_action` description; ARS header reads `"ACTION ARG SCHEMAS (canonical keys for all session-visible actions)"`; W5 listed 97 peer agents; W4/W5/W7 quoted concrete entries (`file__write: {content, path}`, `rag.operation__drop_source: {source}`, `agent.peer__researcher: {request}`) | W1/W2/W3/W4/W5/W6/W7 |
| **Cold-start peer canonical** | B37 `args={"message": ...}` → **B38 `args={"request": ...}`** on `agent.peer__researcher` cold invoke. Description body's hardcoded `args={'message': <user_query>}` text confirmed absent; replaced with `(e.g. {request: ...})` | W5 S3 |
| **S1 `file__write` canonical** | B36/B37 `text` (non-canonical) → **B38 `content` (canonical)**. B34 arg-normalize **did not fire**. permission_denied gate reached correctly. | W4 S1 |
| **R-WEB mcp_search routing chain CLOSED** (= 2-batch chain effect) | B37 `e5ecadb` opened python.unsafe gate; B38 `5e05b9b` added mcp_search to HOT_LIST_SEED. B38 evidence: all 3 R-WEB scenarios now emit `routing_decided{action_name: skill__mcp_search, source: invoke_action, outcome: success}` + `skill_run_spawned`. B37 had zero tool calls (routing miss). | W6 (narr-1 / s-fp11-3 / s-fp12-completion-1) |
| **Ghost rejection (qualified-name corruption)** | B37-OBS-1 `default_api.web__search` 0/17 LLM requests; `[reyn] action_usage: skipping invalid alias 'web_search'` on every turn | W1 |
| **C1 stability + zero latency degradation** at +44% description length | W7 single-agent / 35 turns accumulated, 35/35 clean, 0 empty-stop / 0 G12 Pattern E; p50 latency 4.82s / p90 9.94s identical to B37 W7 | W7 |

### Why these are "verified" despite the V drop

The V count is a single number per scenario representing the entire rubric (= reply quality + events sequence + artifacts). Structural fixes touch one mechanism each. The V count for a scenario can fall while the structural mechanism the fix targets is itself verified, because **another mechanism in the same rubric failed**. Examples from this batch:

- W6 R-WEB scenarios: B37 R (= routing missed entirely) → B38 I (= routing reaches skill_run_spawned, but MCP external infra unreachable). The **routing fix is verified primary data**; the I verdict is downstream of external infra, not the fix scope. The V count didn't move; the structural status did.
- W4 S1: V verdict identical to B37 (= permission gate reached either way); the difference is **arg-normalize handler did not need to fire**, which is the structural improvement (= LLM saw the canonical key directly). V count masks this.

This is the metric-decomposition problem the G31 journal warned about, applied to dogfood V counts.

---

## 2. The headline — G31 lesson direct application

PR #129 (= `2026-05-17-g31-three-component-decomposition.md`, merged into main mid-batch) reports ~430 weak LLM calls across 10 candidate fixes for capability-question leak, finding:

- **C (cap-enum)** = "I can help with..." not a defect; 10/10 invariant across all SP + tool-array ablations
- **No deployable single-PR fix reduces both A (prefix-leak) and B (router-meta-leak)** without offsetting harm
- **ε2** (invoke_action description shortened to 1-line stub) → C-en router-meta 4/10 → **10/10 saturation**
- **ε4** (realistic mid-point description trim) → router-meta 4 → 7 **regression**
- **Surface fungibility**: removing specifics causes the LLM to substitute remaining vocabulary; reducing leak on one surface migrates to another

This was published the same day as B38 fix-wave landing. B38 then **inadvertently created an ε2-inverse-direction natural experiment**: description **grew** from B37 2150ch / B37 3080ch → B38 ~3900–4000ch (= +44% within batch, +86% vs pre-B37). The B38 data shows:

### G31 ε2-direction dilution evidence

**W1 S7 (image generation request)**: B37 V (= LLM declined appropriately) → **B38 R (= LLM asked for details instead of declining)**. Worker hypothesis: ARS description growth dilutes implicit capability-constraint signal. Matches G31's "ε2 inverse" pattern: removing description → router-meta saturation; growing description → capability-decline weakening. Both are description-content-driven attractor changes.

### G31 surface-fungibility evidence (= direct match for "surface area is partly fungible")

| Worker | Scenario | B36 | B37 | B38 |
|---|---|---|---|---|
| W4 | S6 drop_source | `source_id` (arg-key drift) | `source_name` (arg-key drift to new variant) | **wrong action entirely: `skill__index_events`** (action-name drift) |
| W2 | S1 drop_source | — | `source_id` (arg-key) | wrong tool: `operation__create_index` direct invoke |
| W3 | S2 file_glob | `dir` (arg-key) | `dir` (arg-key) | LLM picks `file__list` regardless of ARS (= action selection unchanged) |

The B37 mismatch we structurally fixed (= arg-key layer, via ARS embedding canonical keys) is **not reproducible in B38** because the LLM's wrong-path generation **migrated to other surfaces** (= action name selection, alternate tool routing). This is direct evidence of G31's surface-fungibility hypothesis applied to our fix layer.

### Incomplete-args attractor (= new sub-finding)

**W3 S7 (recall)**: ARS scope expansion made `rag.operation__recall` visible. LLM attempts it but omits the required `sources` arg → `KeyError`. B37 LLM had replied inline avoiding the error entirely, so the V→I shift in B38 is a regression introduced by **expose without enforce**: ARS guides arg names but doesn't guarantee required args are supplied. Same family as W3 S2 / W4 S6 (= ARS expansion exposes more, LLM doesn't use the exposure correctly).

---

## 3. New structural hypothesis (= N=3 same-batch)

> **ARS guides arg names; it does not guide action selection.**

Three observations in B38 supporting this:

1. **W3 S2** (`file_glob_grep`): ARS now lists `file__glob: {pattern, path}` + `file__grep: {pattern, path, glob, ...}` even without hot-list usage. LLM still selects `file__list`. ARS = visibility ≠ preference.
2. **W4 S6** (`drop_source`): ARS lists `rag.operation__drop_source: {source}`. LLM dispatches `skill__index_events{mode: drop}` (= completely different action). ARS canonical key not exercised because action not picked.
3. **W2 S1** (`drop_source` variant): LLM invokes `operation__create_index` directly (= unknown tool name) bypassing the wrapper entirely.

**Implication**: future fixes targeting "LLM picks wrong action" should **not** touch the ARS layer. Action selection is driven by:
- Tool description content (= the part G31 found is at a local optimum; further trim regresses)
- Action frequency / recency (= hot-list ranking, which `freq+recency` already addresses)
- Scenario priors (= the user message text + conversation history)

The B39 fix candidates targeting action selection should reach for these layers, not ARS.

---

## 4. The honest trajectory read

B27 0 → B28 12 → B30 10 → B32 11 → B33 12 → B35 17 → B36 19 → B37 26 → **B38 23**.

### Δ V vs B37: -3 mathematical

Decomposition (= same family as B37 retro §4):

- **Real OS-layer wins**: +4V worth (= W6 phase_no_progress paths +2, W4 S1 canonical, W5 S3 canonical, W6 R-WEB routing chain). Most absorbed by INC verdicts because rubrics couldn't fully complete (= MCP external infra unreachable on W6 R-WEB scenarios, etc.).
- **G31 ε2 dilution side effect**: -1V (W1 S7)
- **Surface fungibility migration**: -3V (W3 S7 incomplete args, W4 S6 action drift, W2 INC→REF x3). The fixes structurally land; the noise surfaces elsewhere.
- **Worktree freshness**: continued -1 to -2V (= unresolved measurement methodology gap from B37 retro §6)

= net -3V with **structural wins under-counted by V metric**. This is the metric-decomposition problem applied recursively to our own batch metric.

### What the V trajectory actually means

The "verified rate" metric is an umbrella. Decomposed:

| Component | B37→B38 trend |
|---|---|
| Wrapper-path arg canonical (= D2-wrapper goal) | ✓ verified structurally |
| Direct-alias schema visibility (= D2-min/D2-full goal) | non-regression |
| R-WEB gate behavior | ✓ unblocked + routing operational (= 2-batch chain) |
| Ghost qualified-name rejection | ✓ structural shape rejection works |
| Hot-list coverage gap | unchanged (= empty-input-schema skills still absent) |
| Description bloat capability dilution | new attractor surfaced (= G31 ε2 evidence) |
| Action-selection attractors | unchanged (= ARS doesn't touch this layer) |
| Worktree freshness V swing | unchanged (= methodology gap) |

The mathematical V drop = sum of all these components weighted by scenario rubric coverage. Stating "B38 verified rate 39.7%" without this decomposition is the umbrella mistake G31 warned about.

---

## 5. Process reflection — what worked

- **Same-day G31 application**: the G31 journal (PR #129) was published mid-batch; user reference + this retrospective applies it to the B38 results without waiting another batch cycle. The "next batch lesson application" loop from B35 → B36 closed faster this time.
- **Canonical journal dir adopted**: all 7 workers wrote to `2026-05-17-batch-38-d2-scope-verify/workers/`. B37 retro §6 recipe gap fixed.
- **B38 fix wave dispatched as 3 sonnets in parallel** with independent scope. judge_phase fix (Shape A structural, no SP change) demonstrated the discipline gate works (= sonnet stop-and-report for SP changes would have fired if needed).
- **LLM-input schema observation** is now standard in worker prompts; every ARS finding was sourced from `dogfood_trace.py --mode llm-tools-schema`.

---

## 6. Process reflection — what didn't work

- **judge_phase fix end-to-end unverifiable** via existing scenario (W3 S8 asks LLM to evaluate inline text; judge_phase requires real artifact file on disk). Unit tests pass; scenario design mismatch blocks e2e verification. Scenario redesign needed.
- **Ghost rejection structural ≠ existence rejection**: `_is_valid_qualified_name` only validates shape (category/separator). `skill__create_skill` passes structural check; only `freq+recency rank=23 outside top-20` saved invocation in B38. Lucky, not structural. B39 candidate: add registry-existence check post-structural validation.
- **Hot-list coverage gap for empty-input-schema skills**: `skill__index_docs`, `skill__read_local_files`, `skill__eval` still absent from hot-list and ARS because their input_schema is empty (= the D2 condition `props non-empty` skips them). Same gap as B36/B37. Need separate mechanism.
- **Worktree freshness V swing methodology**: B37 retro §6 flagged this. B38 didn't address it. Either reset `action_usage.jsonl` deterministically per batch, or annotate hot-list state per scenario in results.

---

## 7. Fix wave priorities for B39+

1. **Ghost rejection: registry-existence check** (= W2 evidence). After structural validation, also check the qualified name resolves to a real action in `KNOWN_STATIC_QUALIFIED_NAMES` + skill / mcp / peer registries. ~20-line patch.
2. **Hot-list coverage for empty-input-schema skills** (= B36/B37/B38 carry-over). Decision: do these skills get a stub schema (= `additionalProperties: true` documented), or get an explicit `args via user_message` marker? Need design call.
3. **W3 S8 judge_phase e2e scenario redesign** — present a real artifact file in the workspace so the postprocessor wrapping fix can be verified end-to-end.
4. **Worktree-freshness measurement methodology** — pick one: deterministic state reset, or per-scenario hot-list annotation.
5. **Action-selection attractor investigation** (= W3 S2 / W4 S6 / W2 S1): per the new N=3 structural hypothesis, ARS is not the right layer. Investigate description content (= G31's local-optimum warning applies, careful) or hot-list freq promotion.
6. **B27-H4 acompletion-never-awaited** (= #52) still open.

---

## 8. Goal restated

Nine batches in. The B27→B37 +26V trajectory was dominated by **structural fixes in envelope layer** (= alias schema, wrapper schema, gate config, eventstore recovery). B37→B38 -3V is the **first batch where the metric direction reverses while structural mechanisms verify**. This is the natural maturation: cheap structural fixes are exhausted, remaining attractor surface is fungible / model-prior-bound (= G31 territory).

B38's contribution to the methodology is **decomposing our own success metric the same way G31 decomposed its target metric**. The "verified rate" was an umbrella that obscured a mix of (a) real OS wins, (b) attractor migration noise, (c) measurement methodology confound. Future batches need component-level metrics, not a single rate.

Target for B39: **stop optimizing the V umbrella**. Instead:
- Track per-mechanism verification (= structural fix landed? primary data quoted? non-regression on adjacent mechanisms?)
- Address the new hypothesis (= ARS doesn't drive action selection) with the appropriate layer
- Close the worktree-freshness methodology gap
- Don't chase a 50% verified rate as a top-line goal; chase **per-component verification** with explicit reporting.
