# Batch 35 — Findings (A2A driver pattern + B34 fix verify + B35 ablation wave)

> Sixth dogfood batch. First batch under the A2A driver pattern (= B33
> W2 F2 follow-up). Main HEAD pre-batch `99d8407`; ablation results
> later integrated against main HEAD `a1a5093` (= mid-window land of
> hot-list alias schema fix by another session).
>
> Headline:
> - Aggregate V/I/R/B = **17/8/29/4** (= verified rate 29.3%, up from
>   B33 12/58 = 20.7%).
> - **Per-fix attribution** confirmed by 3-condition ablation: W7 +3V is
>   100% A2A driver pattern; W3 file__grep routing shift is 100% B34
>   land; W1 -2V is verifier methodology mismatch + LLM noise (not OS
>   regression).
> - **Root-cause discovery (= my session's blind spot)**: B27→B35 で 4 batch
>   累積した `text/content` / `source_id/source` / `dir/path` /
>   `content_regex/pattern` 攻撃面の真因が確定 — `_build_hot_list_aliases`
>   の `parameters: properties: {}` (= alias が target schema を expose せず
>   LLM が arg names を hallucinate / 推測する構造)。 別 session が B35
>   進行中に発見・修正 (= commit `488c15e` D2-min、 `a1a5093` D2-full)。

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD pre-batch | `99d8407` (= post-B34 6-fix wave) |
| HEAD post-batch (= journal landing) | `a1a5093` (= +hot-list alias schema D2-full by another session) |
| Tests | 3401 passed → updates after pull |
| Total scenarios | 58 |
| Workers | 7 sonnet parallel + 3 sonnet ablation = 10 sonnet in-flight |
| Driver | **A2A POST** (= first batch on the new pattern, per dogfood-discipline.md "When to prefer A2A over reyn chat --cui") |
| **Aggregate** | **V=17 / I=8 / R=29 / B=4** |
| Verified rate | **29.3%** |
| Brier mean | not computed (= H5 refit applied B33; per-scenario evidence cited inline instead) |

---

## 1. Per-worker verdict matrix vs B33 baseline

| W | Set | B33 V/I/R/B | **B35 V/I/R/B** | ΔV | Attribution (= ablation-confirmed where flagged ✓) |
|---|---|---|---|---|---|
| 1 | chat_router_smoke | 2/1/4/0 | 0/1/6/0 | -2 | verifier methodology mismatch + LLM ±1V ✓ |
| 2 | stdlib_skills_core | 0/0/9/0 | 2/2/5/0 | **+2** | W2 F2 driver fix (= A2A reply capture) ✓ |
| 3 | control_ir_ops | 3/0/6/0 | 2/0/7/0 | -1 | file__grep routing shift attributable to B34 ✓; arg-name `dir vs path` is the residual |
| 4 | permissions_and_safety | 4/3/1/0 | 3/5/0/0 | -1 | arg-normalize verified 2/2 (= R→V flow); 1V swing within LLM variance |
| 5 | multi_agent_and_mcp | 0/2/5/0 | 2/2/3/0 | **+2** | W5 peer-agent error envelope fix verified ✓ |
| 6 | plan_mode + fp_0011 | 1/4/2/4 | 3/4/2/2 | **+2** | W6 phase_no_progress fix verified ✓; A2A pattern improves spawn-ack narration |
| 7 | long_session_v1 | 2/5/0/0 | 5/0/2/0 | **+3** | A2A driver pattern 100% (= ablation: B34 code 0V contribution) ✓ |
| **Total** | — | **12/15/27/4** | **17/8/29/4** | **+5** | |

Verified rate trajectory: B27 0/58 → B28 12/58 → B30 10/58 → B32 11/58 → B33 12/58 → **B35 17/58 = +5V real**.

---

## 2. B34 fix verification (= per-fix structural)

### 2.1 ✅ W2 F2 driver pattern (A2A migration) — confirmed +2V (W2) + ablation-attributed +4V (W7 isolated)

**Primary data**:
- W2 (4/4 spawn-ack scenarios non-empty reply via A2A): S4 skill_builder produced "新しいスキルが作成されました" (= 83 chars), S5 word_stats correct stat (= 89 chars). B33 W2 had 3/4 empty.
- W7 ablation (= isolating driver from code): A=6V (A2A + post-B34), B=2V (stdin + post-B34), C=6V (A2A + pre-B34). **A vs C identical**, A vs B = +4V. Driver pattern is the sole cause of the W7 improvement.

### 2.2 ✅ W5 peer-agent error envelope — confirmed verified

**Primary data**: W5 S3/S4 traces show `tool_returned: {"status":"error","kind":"agent_not_found","error":"Agent 'researcher' not found in registry.","available_agents":[...]}` — LLM reply now reports the missing agent instead of fabricating ("FP-0001 is an AI fine-tuning agent" hallucination from B33 W5 F2 is gone).

### 2.3 ✅ W6 phase_no_progress completion injection — confirmed verified

**Primary data**: W6 s-fp11-1 events log: `[80] skill_run_failed → [81] skill_completion_injected`. Across all 5 `skill_run_failed` events in the session, every one followed by `skill_completion_injected`. B33 had this skipped on the phase-loop rollback path.

### 2.4 ✅ arg-normalize (file__write text→content, drop_source source_id→source) — confirmed verified

**Primary data**: W4 S1 `{text, path}` → normalized → `_handle_write` reached the permission gate → `permission_denied` for `/etc/test.txt`. B33 W4 S1 had `KeyError: 'content'` aborting before the gate. S6 analogous for `drop_source` / `source_id` → `permission_denied (kind: index_drop)`.

### 2.5 ⚠️ file__grep / file__glob handler — partial; routing fixed, arg-name gap surfaced

**Routing primary data (= W3 ablation)**:
- Condition A post-B34 N=5: 3/5 → `file__glob`, 0/5 → `file__list`
- Condition B pre-B34 N=5: 0/5 → `file__glob`, 0/5 → `file__list`, 1/5 → `file__grep` (UnknownActionError)
- → **B34 land is the sole cause of the routing shift** (HIGH).

**B33 baseline reinterpretation**: B33 W3 S2's observed `file__list` call was a fallback after `file__grep` failed silently in pre-B34 state. The LLM's true first preference is `file__grep`; B34 makes that preference reachable.

**Residual arg-name gap**: LLM sends `dir` as the directory argument to `file__glob`, but `GLOB_FILES` schema uses `path`. This is the same root cause as `text/content` and `source_id/source`: alias schema empty. Addressed by the hot-list alias schema fix landed mid-window (§ 2.6).

### 2.6 ✅ Hot-list alias schema empty (= root cause discovered mid-batch by another session)

**Primary data (= my session's blind spot, recorded for memory)**:
- B27→B35 で 4 batch にわたり同 class attractor (= `text vs content` / `source_id vs source` / `dir vs path` / `content_regex vs pattern`) を観測。
- 私の session は 4 件すべてを「individual synonym normalization」 で対応(B34 arg-normalize 含む)。 真因に至らず。
- 別 session が `src/reyn/chat/router_loop.py:_build_hot_list_aliases` を直接読み、 `parameters: {"type": "object", "properties": {}, "additionalProperties": True}` (= 空 schema) を発見。
- LLM は hot-list direct alias を呼ぶとき、 target tool の input_schema を一切見られない → arg names を hallucinate / 推測する。
- D2-min (= `488c15e`) で operation-category alias、 D2-full (= `a1a5093`) で resource-category alias の target schema を embed。

**Implication for B34 arg-normalize fix**: D2-min/D2-full の root-cause fix が land した今、 B34 arg-normalize (= text→content, source_id→source の handler-side defensive) は **redundant 寄り** (= LLM が schema を見て canonical key を最初から選ぶようになるため、 handler 側で synonym 受容する path に到達しにくくなる)。 ただし即時 revert は不要 — defense-in-depth として残しても害なし、 B36 retest で synonym 経由 invocation の頻度を観測してから取り扱いを決める。

**Lessons logged to memory**:
- `feedback_envelope_layer_fix.md` (= scope 拡張): handler-side defensive は対症療法、 「LLM input 整形 (= alias / tools array schema)」 が first-class envelope の真の sublayer。
- `feedback_llm_input_schema_observation.md` (= 新規): worker prompt の verification angles に `dogfood_trace.py --mode llm-tools-schema` を必須化。
- `feedback_cross_batch_pattern_threshold.md` (= 新規): N≥3 同種 observation で「共通真因」 hypothesis を必ず立てる。 「1 fix 1 検証」 を局所最適化して個別 fix を累積するのは anti-pattern。

### 2.7 ✅ task #93 verifier triad integration — landed (W4 framework-side bug surfaced)

**Primary data (W4)**: framework reported `0V/8I/0R/0B` while manual rubric was `4V/4I/0R/0B`. Note: this was B33 W4's finding — B34 commit `fb42a05` wired the triad. B35 W4 manual rubric matched framework-reported counts (= within ±1, no methodology mismatch).

---

## 3. Ablation wave — 3 hypotheses, 3 confirmed attributions

### 3.1 W7 (+3V trajectory) — A2A driver pattern is the sole cause [HIGH]

Conditions (N=3 × 7 scenarios = 21 shots each):

| Condition | Driver | Code | V/I/R/B |
|---|---|---|---|
| A baseline | A2A POST | post-B34 | 6/15/0/0 |
| B A2A isolated | stdin-pipe | post-B34 | 2/15/4/0 |
| C code-fix isolated | A2A POST | pre-B34 (reverted) | 6/15/0/0 |

**A vs C identical (V=6 both)**: B34 code fixes contribute **0V** to long_session_v1. The B34 changes (phase_no_progress / peer-agent / arg synonym / file__grep) do not touch any code path exercised by these scenarios.

**A vs B Δ=+4V**: driver pattern alone. Key mechanism: under stdin-pipe, S5 (`general_python_chain`) returned narration only (= "The tool successfully generated..."), the actual asyncio.Queue code (5000+ chars) never reached the agent reply. Under A2A POST, full skill output is captured in the reply. This is B33 W2 F2 re-confirmed on long_session.

### 3.2 W3 (file__glob routing shift) — B34 land is the cause [HIGH]

Conditions (N=5 each):

| Condition | first-turn tool | count |
|---|---|---|
| A post-B34 | `file__glob` | 3/5 |
| A post-B34 | no-tool-call (inline reply) | 2/5 |
| B pre-B34 | `file__glob` | 0/5 |
| B pre-B34 | `file__grep` via invoke_action (UnknownActionError) | 1/5 |
| B pre-B34 | `list_actions` | 1/5 |
| B pre-B34 | no-tool-call | 3/5 |

**Attribution**: B34 file__grep / file__glob ToolDefinitions + re-seeded `DEFAULT_HOT_LIST_SEED` is the sole cause of the routing shift (HIGH).

**Synonym sub-finding**: LLM sends `dir` as the directory argument 2/5 times (= addressed by D2-min/D2-full alias schema fix, not by handler-side synonym). The `content_regex` hypothesis from B35 worker observation was **refuted** by the ablation — LLM sends `pattern` + `content_regex` together; handler uses `pattern`, ignores extra.

### 3.3 W1 (-2V drop) — verifier methodology mismatch + LLM noise [HIGH]

Conditions (N=3 each × 7 scenarios):

| Condition | Driver | Wipe | V/7 mean |
|---|---|---|---|
| A B35 reproduction | A2A per-fresh agent | per-scenario fresh | 4.3 |
| B legacy + full wipe | stdin-pipe | events wiped | 3.7 |
| C A2A + bad wipe | A2A (same agent) | rm -rf events | 1.0 (= EventStore stale-path crash) |

**Attribution**:
- B35 W1 V=0 is **not reproduced** under condition A (= 4.3V/7). Rules out A2A pattern as the cause.
- **Primary cause**: artifact verifier strictness gap — B35 W1 scored 5/7 scenarios `artifacts_pass=false` because the `direct_llm` skill answers inline (= no physical artifact file), but the scenario YAML declares `{skill: direct_llm, present: true}`. B33 W1 used permissive manual assessment skipping artifact check for inline-reply scenarios → 2V. Methodology mismatch.
- **LLM variance**: ~±1V (= S6 multi-turn: 1/3 in A, 2/3 in B).
- **EventStore stale-path bug confirmed** (condition C): `rm -rf .reyn/events/` while web server is live → `EventStore._active` holds stale path → next write FileNotFoundError. Stack: `session._handle_user_message → _chat_events.emit → EventStore.write → _active.open("a")`. Separate issue, MED severity.

---

## 4. New findings surfaced in B35

### 4.1 [HIGH-root-cause-landed-elsewhere] hot-list alias schema empty (§ 2.6)

Already addressed by `488c15e` + `a1a5093`. My session's blind spot recorded above + in memory.

### 4.2 [MED-new-issue] EventStore stale-path crash on events wipe with live server

W1 ablation condition C confirmed. `rm -rf .reyn/events/` while `reyn web` is live → next emit raises `FileNotFoundError` at `event_store.py:61`. Affects dogfood scenarios that wipe events between turns under the A2A driver pattern.

**Fix sketch**: `EventStore.write()` catches `FileNotFoundError`, resets `_active = None`, calls `_open_new_file()` on retry. Severity MED (only fires in dogfood / scripted env, not in production).

### 4.3 [HIGH-pre-existing-reproduced] `simple_memo_app` LLM attractor (W7)

LLM consistently picks `skill__simple_memo_app` in response to: "tell me more about the simplest one", "give me an example", open-ended final-turn questions. Affected 7 of 37 turns in W7 (= S2-T2, S3-T5, S5-T5, plus S2-T3 through T6 downstream contamination). Hypothesis: skill description contains "simple" or catalog position makes it the lowest-barrier selection target. **Description audit candidate**.

### 4.4 [HIGH-routing] `mcp_install` request → `mcp_search` routing (W5 S6, W6 s-fp12-completion-1)

LLM routes mcp_install intent to `mcp_search` skill (= description-class collision similar to the B23-PRE-1 / 4-way skill audit pattern). Persistent across batches.

### 4.5 [HIGH-routing] `list_actions(filter=<path>)` for directory listing (W2 S3, S9, W4 if any)

LLM uses `list_actions` with path-shaped `filter` for directory listing tasks. `file__glob` is in the catalog post-B34 but the LLM doesn't always choose it. Reproduced from earlier batches.

### 4.6 [MED-rubric] artifact assertions for inline-reply scenarios

Scenario YAMLs declare `{skill: direct_llm, present: true}` for scenarios where the LLM should answer inline. Inline replies don't produce artifacts. Either (a) rubric should accept `artifacts: present=false OR skill_run_spawned=false` (= "no skill ran is OK if reply rubric pass"), or (b) the scenarios should be split.

---

## 5. Severity rollup

### CRITICAL — none

### HIGH — fix candidates for B36+ (= post-pull state)

| ID | Source | Direction |
|---|---|---|
| §4.2 | EventStore stale-path | `EventStore.write()` recovery + `_open_new_file()` retry |
| §4.3 | `simple_memo_app` attractor | description audit |
| §4.4 | mcp_install routing mistake | description audit (= 5-way: builder / improver / importer / eval / install) |
| §4.5 | `list_actions(filter=<path>)` recurrence | envelope-layer empty-result hint (= "for filesystem listing, use file__list") OR scenarios-side audit |

### MED — follow-ups

| ID | Source | Direction |
|---|---|---|
| §4.6 | artifact strictness for inline replies | rubric or scenario refactor |

### Resolved during the batch window (= recorded for trajectory)

- §4.1 hot-list alias schema empty (commits `488c15e` + `a1a5093` by another session)
- B34's 6 fixes (= W2 F2 driver / W5 peer / W6 phase / arg-normalize / file__grep / verifier-integration) all per-fix verified.

---

## 6. Process reflection

### What worked

- **A2A driver pattern** delivered immediately. W7 alone +4V (ablation-isolated). W2 F2 reply-capture gap closed across 4/4 spawn-ack scenarios.
- **3-condition ablation** (= driver + code revert + driver isolation) gave HIGH-confidence attribution for the +5V trajectory. No "副作用" inference paragraphs this batch.
- **Mid-window land of root-cause fix** (= alias schema by another session) shipped without colliding with this session's work (= no file conflict on merge).

### What didn't work (= my session's blind spot, lesson logged)

- **Cross-batch pattern recognition skipped**. 4 batches of `arg-name mismatch` (= text/content, source_id/source, dir/path, content_regex/pattern) observed but treated as 4 independent synonym fixes. Should have hit the N≥3 threshold and asked "is there a single structure that explains all of these?" Then a single trace of `dogfood_trace.py --mode llm-tools-schema` on any one alias would have surfaced the empty-properties root cause.
- **`feedback_envelope_layer_fix.md` scope-narrowing**. I read "envelope-layer" as "handler-side defensive (= synonym 受容)" and missed the true envelope sublayer: LLM input schema (= alias parameters). The memory has been updated to widen the definition.
- **Trace tool feature unused**. `scripts/dogfood_trace.py --mode llm-tools-schema` was already implemented but not exercised in any worker prompt. Memory `feedback_llm_input_schema_observation.md` added to active-trigger this for future batches.

### What needs adjustment for B36+

- **Worker prompts**: add "LLM-input schema observation" as a required verification angle for any wrong-arg / wrong-tool / hallucinated-name finding. Quote the relevant alias's `parameters.properties` alongside the LLM's tool_calls.
- **Cross-batch pattern audit**: include a "same-class observation count" line per finding in the journal so retrospective can detect the N≥3 threshold.

---

## 7. Cross-reference

- Worker artefacts: `workers/findings-worker-{2,3,4,5,6,7}.md` + `workers/results-worker-{1..7}.json` (= W1 wrote results.json + inline summary only)
- Ablation: `ablation/W1-attribution.md`, `ablation/W3-attribution.md`, `ablation/W7-attribution.md`
- Memory lessons logged this batch:
  - `feedback_envelope_layer_fix.md` (= scope 拡張: schema layer first-class)
  - `feedback_llm_input_schema_observation.md` (= 新規: active trigger)
  - `feedback_cross_batch_pattern_threshold.md` (= 新規: N≥3 threshold)
- B34 commits verified e2e: `98ab4c5` / `c5ecf91` / `f8157b3` / `fb42a05` / `4d4869d` / `265a430`
- Root-cause fix landed by another session: `488c15e` (D2-min) + `a1a5093` (D2-full)
- Open follow-up issues: #52 (B27-H4 acompletion never awaited) — not retested
