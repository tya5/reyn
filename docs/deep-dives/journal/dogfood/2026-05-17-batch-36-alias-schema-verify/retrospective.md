# Batch 36 — Retrospective

> Seventh dogfood batch. **Alias schema D2-min/D2-full** (= the other
> session's mid-B35 land) **verified end-to-end via direct trace
> observation of `parameters.properties` non-empty across W1/W2/W3/W5**.
> Verified rate ticked up to **32.8%** (= 19/58), net +2V vs B35 (+6V
> excluding W7's rubric-scope artifact). The headline finding is **not**
> the schema fix itself — it is the **dual-surface envelope-layer gap**
> the schema fix exposed.

---

## 1. What this batch verified

### Verified — direct primary data (= `dogfood_trace.py --mode llm-tools-schema`)

- **D2-min** (= operation-category alias schema): W1 confirmed
  `web__search.parameters.properties = {query, max_results}`, non-empty.
- **D2-full** (= resource-category alias schema): W1/W2/W3/W5 confirmed
  - `file__read.parameters.properties = {path}`
  - `file__grep.parameters.properties = {pattern, path, glob, case_sensitive, max_results}`
  - `file__glob.parameters.properties = {pattern, path}`
  - `skill__skill_builder.parameters.properties = {skill_name, description, goal}`
  - `agent.peer__researcher.parameters.properties = {request}` (= `to` field dropped)
- **arg-name mismatch resolved on direct alias path**: W1/W2/W3 saw the
  LLM pick the canonical key (`pattern`, `query`, `skill_name`,
  `description`, `goal`, `request`, `path`) on every direct hot-list
  alias invocation. The B33–B35 attractor (`text` / `source_id` / `dir`
  / `content_regex` / `message`) is structurally gone for this path.
- **W3 cluster trajectory reversed**: B28=6V → B30=2V → B32=2V → B33=3V
  → B35=2V → **B36=4V** (= first increase since B28, +2V attributed to
  D2-full).
- **EventStore stale-path recovery** (B35 W1 ablation cond C):
  `EventStore.write()` now catches `FileNotFoundError`, resets `_active`,
  retries `_open_new_file()` once. Tier 2 test
  `tests/test_event_store_stale_path_recovery.py` (2 cases, no mocks).

### C1 / Q2 stability (= continued)

- W7 long_session: **37/37 turns clean**, 0 empty-stop, 0 G12 Pattern E.
- W6 spawn-ack `/tasks` pointer present across plan + fp_0011 scenarios.

---

## 2. The headline finding — dual-surface envelope-layer gap (= N≥3)

The D2-min/D2-full fix embeds the target ToolDefinition's schema into
**direct hot-list alias entries**. It does **not** propagate into the
**`invoke_action` wrapper path**, where the inner `args` schema remains
`{additionalProperties: true}` and the LLM still has to guess parameter
names.

Cross-batch + cross-worker same-class observations (= 2026-05-17 B36):

| Worker | Scenario | Direct alias path | `invoke_action` wrapper path |
|---|---|---|---|
| W1 | S0–S2 | `query` ✓ (web_search) | n/a |
| W2 | skill_builder | `{skill_name, description, goal}` ✓ | n/a |
| W3 | S2 file_glob_grep | `pattern` ✓ | n/a |
| W4 | S1 file_write | n/a | `{text, path}` ✗ → B34 normalize fired |
| W4 | S6 drop_source | n/a | `{source_id}` ✗ → B34 normalize fired |
| W5 | S3 multi_agent | n/a | `{message}` ✗ (vs canonical `request`) |

= **N=3 invoke_action-wrapper-path mismatches in a single batch**, all
of which the D2 fix could not reach. By the cross-batch threshold rule
(memory: `feedback_cross_batch_pattern_threshold` — N≥3 same-class
observations trigger a structural hypothesis), this is **not** a set of
local hallucinations. The structural hypothesis is:

> **invoke_action wrapper's `args` should expose the same schema as the
> direct alias** (= D2-min/D2-full propagation into wrapper invocation
> path).

The B34 arg-normalize handler-side defensive (= `text→content`,
`source_id→source`) is therefore **not redundant** — it is the
defense-in-depth that covers the wrapper path. Both fixes co-exist
without conflict; the next structural fix is to extend D2 into the
wrapper.

### Why this matters

This is the same memory lesson the B35 retrospective surfaced
(`feedback_llm_input_schema_observation` + `feedback_envelope_layer_fix`
scope expansion), now applied **inside the batch** rather than after.
Every worker prompt this batch carried the explicit angle "use
`dogfood_trace.py --mode llm-tools-schema` for any wrong-arg / wrong-tool
finding", and all three invoke_action-path findings (W4 S1, W4 S6, W5
S3) included the LLM-input-schema excerpt as primary data. The
mechanism that produced the B35 blind spot was the **absence** of this
verification angle in worker prompts. B36 closes that loop.

---

## 3. The honest trajectory read

B27 0/58 → B28 12/58 → B30 10/58 → B32 11/58 → B33 12/58 → B35 17/58 →
**B36 19/58 = 32.8%**.

The +2V net from B35→B36 decomposes:

- **D2-min/D2-full direct-alias coverage**: +6V across W2 (+3) / W3
  (+2) / W5 (+1). Primary data = schema excerpts + canonical-key
  tool_calls.
- **W6 fp_0011/fp_0012 routing improvement**: +2V (= s-fp12-completion-1
  fixed `list_actions` mis-route from B35; now correctly invokes
  `invoke_skill`).
- **W7 rubric-coverage artifact**: -4V (= B35 had 7 rubric-eligible
  scenarios, B36 has 2 because long_session yaml moved most scenarios
  to "no rubric" smoke-only). Not an OS regression. C1=37/37 clean,
  zero empty-stop.
- **W6 R-WEB-TRUSTED-PYTHON gate (new finding)**: -3 to BLOCKED
  (= `web/deps.py` hardcodes `PermissionResolver(unsafe_python_allowed=False)`,
  ignoring `python.unsafe: allow` in config). Surfaced because the
  scenarios reach the gate; the gate itself is the bug.

Real OS-layer wins (= ablation-grounded or direct-trace verified):
+8V across W2/W3/W5/W6. Scenario-rubric churn / new-finding BLOCKED:
-3 to -7V. Net +2V.

---

## 4. Process reflection — what worked

- **LLM-input schema verification angle, now standard**: every worker
  prompt this batch included the explicit
  `dogfood_trace.py --mode llm-tools-schema` requirement. All three
  invoke_action-path findings (W4 S1, W4 S6, W5 S3) carry the schema
  excerpt as primary data, alongside the tool_call args. The B35
  blind-spot mechanism is closed at the worker-prompt-template layer.
- **Cross-batch pattern threshold, applied inside batch**: N=3
  invoke_action-path same-class observations in a single batch hit
  the threshold rule the same day it was logged
  (`feedback_cross_batch_pattern_threshold`, B35 retro). The structural
  hypothesis (= invoke_action wrapper schema propagation) was identified
  at aggregate time, not 4 batches later.
- **A2A driver pattern is stable** across all 7 worker concurrencies.
  Documented recipe (= `dogfood-discipline.md`) was followed cleanly
  with no driver-related findings.
- **EventStore stale-path fix landed cleanly** with Tier 2 regression
  test, no mocks, bounded one-retry contract.

---

## 5. Process reflection — what didn't work

- **Hot-list coverage gap (W2)**: `skill__eval`, `skill__direct_llm`,
  `skill__index_docs`, `skill__read_local_files` are usage-seeded and
  never present in a fresh-workspace hot-list. D2-full cannot help if
  the alias isn't in the list. This is a **separate structural gap**
  from the wrapper-path gap; both need addressing.
- **R-WEB-TRUSTED-PYTHON gate bug (W6)**: hard-coded
  `unsafe_python_allowed=False` in `web/deps.py` ignores config. New
  HIGH-severity finding, surfaced only because B36 scenarios reach the
  gate. Symptom: 3 verdicts moved INCONCLUSIVE→BLOCKED in W6 aggregate.
- **W6 narration mischaracterisation** (s-fp11-1): "file read
  restriction" used instead of "invalid circular graph spec". Error
  surfaced correctly + anti-optimism held, but the surface description
  is wrong. LOW severity, description-layer issue.
- **Scenario design verification gap (W1 S4)**:
  `skill_run_completed.status="finished"` (runtime) vs `success`
  (rubric yaml) — scenario yaml drift, not a runtime regression. Audit
  of `live_runner.py` rubric matching against actual event vocabulary
  needed.

---

## 6. Fix wave priorities for B37+

In priority order (= structural where possible):

1. **invoke_action wrapper schema propagation** (= the B36 headline
   structural hypothesis). Extend D2-min/D2-full so that
   `invoke_action({action_name, args})` exposes the target action's
   schema inside `args`, the same way direct hot-list aliases do.
   Verifies arg-name mismatch is structurally resolved on **both**
   surfaces. If verified, B34 arg-normalize handler-side defensive
   becomes redundant.
2. **R-WEB-TRUSTED-PYTHON gate config integration** (= W6 new HIGH).
   `web/deps.py` should resolve `python.unsafe` from config, not
   hardcode False. ~10-line fix.
3. **Hot-list usage-seeded gap** (= W2 + B27 historical). For
   fresh-workspace scenarios, seed list with the catalog's commonly-
   exercised skills (= `skill__index_docs`, `skill__read_local_files`,
   `skill__direct_llm`, `skill__eval`). Already started in B28 / B30 /
   B34 (= `DEFAULT_HOT_LIST_SEED` growth); audit gap items vs current
   seed.
4. **Scenario rubric audit** (= W1 S4): live_runner verifier rubric
   matching against actual emitted event vocabulary. `success` vs
   `finished` is one instance; sweep for others.
5. **`simple_memo_app` attractor follow-up** (= B35 §4.3 listed). B36
   non-recurrence is confounded by skill not being in the catalog;
   re-test once skill is back in scope.
6. **`mcp_install` → `mcp_search` routing collision** (= B35 §4.4): N=1
   non-recurrence in B36 (W5), not confirmed resolved. Re-test under
   the next batch.
7. **B27-H4 acompletion-never-awaited** (= #52, deferred).

---

## 7. Goal restated

Seven batches in: the structural fix wave continues to land cleanly
with ablation-grounded or direct-trace attribution. The discipline
layer added this batch is **same-batch cross-pattern recognition**
(= the N≥3 invoke_action-wrapper-path observation triggered the
structural hypothesis at aggregate time, before any individual fix
proposal). The user's "trace tool で context 分析、 patch 切り分け済み
ですか?" challenge from B35 is now operationalised in the worker
prompt template; the next loop is **structural hypothesis at aggregate
time, not retrospective time**.

Target for B37: invoke_action wrapper schema fix verifies arg-name
mismatch is resolved on the wrapper path; R-WEB-TRUSTED-PYTHON gate
config integration verifies the 3 BLOCKED verdicts unblock; net
verified rate above 35%.
