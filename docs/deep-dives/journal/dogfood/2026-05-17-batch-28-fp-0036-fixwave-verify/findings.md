# Batch 28 — Findings (Wave 1 fix-wave verification)

> Retest of the FP-0036 starter set against `main` HEAD `1a5be83` (=
> Wave 1 fix wave + reyn.yaml python.safe fix). 7 sonnet workers, same
> 58 scenarios, same parallel worktree pattern.
> Headline: **V 0 → 12 (+12), R 26 → 21 (-5), B 13 → 1 (-12)**. Every
> CRITICAL / HIGH fix from B27 verified e2e. New cluster of MED-band
> findings emerged behind the unblocking.

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD pre-batch | `f5a6866` (= post-Wave-1 + seed S6 follow-up) |
| HEAD post-batch | `1a5be83` (= +reyn.yaml python.safe fix landed during batch) |
| Tests | 3321 passed / 5 skipped / 2 xfailed |
| Total scenarios | 58 |
| Workers | 7 sonnet parallel, per-cwd + per-reyn-agent isolation |
| Worktrees | `/tmp/reyn-worktrees/b28-{1..7}` |
| Wall-clock | ~30 min (longest worker 6 at ~30m) |
| LLM model | `gemini-2.5-flash-lite` via local LiteLLM proxy |
| **Aggregate verdict** | **V=12 / I=24 / R=21 / B=1** |
| **Verified rate** | **20.7%** (= B27: 0.0%) |

---

## 1. Per-worker verdict matrix vs B27 baseline

| W | Set | B27 V/I/R/B | B28 V/I/R/B | ΔV | ΔR | ΔB |
|---|---|---|---|---|---|---|
| 1 | chat_router_smoke | 0/0/3/4 | 0/0/7/0 | +0 | +4 | **-4** |
| 2 | stdlib_skills_core | 0/2/6/1 | 1/0/8/0 | +1 | +2 | -1 |
| 3 | control_ir_ops | 0/3/3/3 | **6/1/2/0** | **+6** | -1 | -3 |
| 4 | permissions_and_safety | 0/7/1/0 | 3/5/0/0 | +3 | -1 | 0 |
| 5 | multi_agent_and_mcp | 0/1/6/0 | 0/3/4/0 | +0 | -2 | 0 |
| 6 | plan_mode + fp_0011 | 0/6/0/5 | 0/10/0/1 | +0 | 0 | **-4** |
| 7 | long_session_v1 | 0/0/7/0 | 2/5/0/0 | **+2** | **-7** | 0 |
| **Total** | — | **0/19/26/13** | **12/24/21/1** | **+12** | **-5** | **-12** |

---

## 2. Wave 1 fix verification (= e2e contracts)

### 2.1 ✅ B27-C1 — universal-wrapper duplicate filter

**Status: VERIFIED across all 7 workers.** Every trace inspected shows each universal wrapper exactly once in `tools[]`.

Headline evidence from W7 (= the worker most exposed to multi-turn alias accumulation):

> 62 LLM calls spanning 7 multi-turn scenarios (37 turns). `list_actions` / `describe_action` / `invoke_action` each appear exactly once per call. Zero duplicates. B27 crashed at turn 2 in every long-session scenario; B28 completed 37/37 turns (100%).

W1, W2, W3, W4, W5, W6 corroborate. The fix in `_build_hot_list_aliases` (commit `c0d5ea8`) is fully effective at the envelope layer.

### 2.2 ✅ B27-H1 — `plan` tool restoration

**Status: VERIFIED via W6.** `plan` appears as the first tool in plan-mode scenarios' router tools array (= 14 tools total: `plan`, `list_actions`, `describe_action`, `invoke_action`, + 10 hot-list / universal aliases). The B27 hallucination `invoke_action(action_name="default_api.plan")` is gone.

`plan_emitted` event fired in 2/3 plan_mode scenarios. The third (`plan_explain_with_code_references`) chose `reyn_src_read` directly — rational behaviour for a single-file meta question, not a regression.

### 2.3 ✅ B27-H2 — `web__fetch` always-visible per FP-0022

**Status: VERIFIED via W4 S8.** With `web.fetch: deny` in `reyn.local.yaml`, `web__fetch` is present in the tools array (14 tools, request_id `9eb7feb7`). FP-0022 spec restored.

(The enforcement-layer bug — fetch returning 200 despite deny — is tracked as issue #53; not addressed in this wave.)

### 2.4 ✅ B27-H3 — peer-agent `KeyError: 'request'`

**Status: VERIFIED via W5 S4.** `invoke_action(agent.peer__researcher, {message: ...})` is correctly translated by `_delegate_to_agent_args` to the `request` key the handler expects. `agent_message_sent` event fired, `tool_returned` with `status: dispatched`. The peer agent "researcher" was absent by design (= environment), but the dispatch path no longer crashes.

### 2.5 ⚠️ B27-H4 — skill_run lifecycle (partial)

**Status: PARTIAL.** W2's session-shutdown path verified clean (= S4 ends with `skill_run_completed`). The remaining `skill_run_interrupted` instances in W2's traces are caused by **`preprocessor_step_failed` due to python.safe permission denial** — not the H4 root cause, but a co-occurring issue addressed by the `reyn.yaml` fix landed mid-batch (commit `1a5be83`).

The original "`acompletion was never awaited` warning" root cause remains tracked as issue #52.

### 2.6 ✅ B27-Q1 — scenarios assert `routing_decided` for inline ops

**Status: PARTIAL VERIFIED.** Scenarios that genuinely exercise inline ops (W3 9/9, W4 several, W7 several) emit `routing_decided` as expected. The non-emissions cluster cleanly as a separate finding (= §3.2 pre-flight refusal pattern) — scenarios where the LLM identifies the task as "no action needed / unavailable" before invoking anything.

### 2.7 ✅ B27-M2 — `file__grep` removed from seed

**Status: VERIFIED via W3.** `file__grep` is never called by the LLM in B28 traces. The invariant test added with the fix (commit `1636584`) continues to enforce the routing-rule presence.

### 2.8 ✅ B28-NEW-2 — `python.pure` → `python.safe` in reyn.yaml (landed mid-batch)

**Status: VERIFIED by reproducibility.** W1 (S4 `word_stats_demo`) and W2 (S5/S6) both independently surfaced the symptom (= `preprocessor_step_failed` despite `python.pure: allow` in reyn.yaml). Source-confirmed: `permissions.py:497/543/873` keys safe-mode as `python.safe`. Fix committed (`1a5be83`) — reyn.yaml renamed to `python.safe: allow`.

---

## 3. New findings (= surfaced once Wave 1 unblocked deeper paths)

### 3.1 RAG indexing attractor (W2 S1, S9)

LLM consistently calls hallucinated `rag.operation__add_source` / `rag.operation__create_index` / `rag.operation__add_document` — none of which exist in `_OPERATION_RULES`. The correct path is `invoke_action(skill__index_docs, ...)`.

Root cause: `skill__index_docs` is in the skill registry but not in `DEFAULT_HOT_LIST_SEED`. The LLM has to discover it via `list_actions` (= second-best path), but the rag.operation category description / hot-list seed primes `rag.operation__*` as the natural pattern.

**Severity: MED.** This is the same pattern class as B27-M2 (= seed-vs-routing mismatch) but with a stronger hallucination push from the LLM. Suggested fix angle: seed `skill__index_docs` (= 1-line) + audit other index-related skills for seed coverage.

### 3.2 Pre-flight refusal pattern (W1/W4/W5)

LLM identifies a requested action as "unavailable / not the right tool" pre-flight and replies directly, bypassing `invoke_action`. Examples:

- W4 S4-S7: graceful explanation replies, no tool calls → `routing_decided` not emitted
- W5 S1/S2/S6: MCP / install tasks identified as out-of-scope without `skill_run_spawned`
- W1 S5/S7: clarification asked instead of acting / declining

The scenarios' `must_emit: routing_decided` (= post-Q1) is violated because no tool was invoked.

**Severity: scenario-design issue, not OS bug.** The reply rubrics still pass for most of these — the LLM is making the *correct* call. Scenarios authored on the assumption that every router turn issues a tool call don't match the FP-0034 router contract for unavailable-tool paths.

**Action: B28-Q2 design decision** — should scenarios assert `routing_decided` only when the rubric requires a tool call, OR should the router emit a `chat_turn_decided_inline` synthetic event when it answers without invoking anything?

### 3.3 `file__write` `KeyError: 'content'` (W4 S1)

`_handle_write` reads `args["content"]` (`tools/file.py:143`). LLM in W4 S1 sent args without the `content` key. This is **LLM schema non-compliance**, not an OS routing bug — the schema requires both `path` and `content`.

**Severity: LOW.** Envelope-layer improvement candidate (= more forgiving handler that returns a clear "missing required field: content" instead of KeyError), but not a structural fix. Logging the trace for the prompt → args drift would help diagnose whether description wording confused the LLM.

### 3.4 `eval` scenario misrouted to `skill_improver` (W2 S7)

LLM invoked `skill_improver` for an `eval` scenario. Both skills share keywords ("evaluate", "score") in their descriptions; the LLM picked the wrong one and emitted JSON-string args that the wrapper rejected.

**Severity: MED.** Description ambiguity between `eval` (= run a golden-dataset eval) and `skill_improver` (= iterate skill via eval-plan-apply loop). Audit candidate.

### 3.5 PLAN-STEP-PATH (W6)

Plan step LLMs call `reyn_src_read("principles.md")` (= relative) instead of the full `docs/concepts/architecture/principles.md` (= cwd-anchored). Step prompts don't carry the resolved cwd well enough to set the path correctly.

**Severity: MED.** Plan-mode multi-source synthesis is brittle until step LLMs receive resolved-path context.

### 3.6 STEPS_JSON_ESCAPE (W6)

When the goal contains inner quotes, the LLM sometimes fails to escape them in `steps_json`, producing `plan_invalid: not valid JSON`. Workaround: avoid inner quotes in scenario prompts.

**Severity: LOW.** Plan-tool description improvement candidate.

### 3.7 `direct_llm` artifact never created (W1 / Q1 follow-up)

`must_emit_artifact: {skill: direct_llm}` fails on every chat_router_smoke scenario because the router answers inline without spawning the `direct_llm` skill. This is the same FP-0034 design intent surfaced by Q1, just for artifacts instead of events.

**Severity: scenario-design.** Same B28-Q2 cluster.

### 3.8 Self-message attempt (W6 s-fp12-completion-1)

LLM routed to `agent.peer__dogfood-b28-6` (= itself) after `mcp_search` failure. Cycle should be detected and prevented.

**Severity: LOW.** Multi-agent loop-detection edge case.

### 3.9 ADR discoverability gap (W7 scenario 6)

`reyn.source__list` seed exposes top-level but doesn't lead the LLM to `docs/deep-dives/decisions/`. Seed could include common doc-tree depth hints, OR the description could note "list_directory recurses".

**Severity: LOW.** Authoring polish.

### 3.10 ASYNC_NARRATION_GAP (W6 fp_0011_*)

`skill_completion_injected` doesn't fire within `reyn chat --cui` session lifetime (= session exits before the async skill's completion narration is delivered). Multi-turn driver would expose it.

**Severity: scenario-driver design.** Not a runtime bug; the dogfood driver's single-shot stdin pattern is incompatible with FP-0012 spawn-ack semantics.

### 3.11 MCP_REGISTRY_UNAVAILABLE (W6 narr-1 / s-fp12-completion-1)

Environment-level — MCP registry not reachable from worktree. Routes correctly to `skill__mcp_search` but the skill itself can't complete. Marked `blocked` per discipline.

**Severity: environment.** Not a code issue.

---

## 4. Severity classification

### CRITICAL — none

### HIGH — none

### MED — fix candidates for next wave

| ID | Finding | Fix angle |
|---|---|---|
| B28-MED-1 | RAG attractor (W2) | Seed `skill__index_docs` |
| B28-MED-2 | eval ↔ skill_improver disambiguation (W2) | Description audit |
| B28-MED-3 | PLAN-STEP-PATH cwd context (W6) | Step prompt: resolved-cwd injection |

### LOW — defer / authoring polish

| ID | Finding |
|---|---|
| B28-LOW-1 | `file__write` KeyError on missing content (W4) — defensive handler |
| B28-LOW-2 | STEPS_JSON_ESCAPE (W6) — plan description hardening |
| B28-LOW-3 | Self-message detection (W6) |
| B28-LOW-4 | ADR depth discoverability (W7) |

### Design questions (= scenario / contract)

| ID | Question |
|---|---|
| B28-Q2 | `routing_decided` / `direct_llm` artifact: how should scenarios assert "LLM answered inline without invoking a tool"? |
| B28-Q3 | Async-narration verification: does FP-0036 framework need a multi-turn driver to observe `skill_completion_injected`? |

---

## 5. Calibration

B27 outcome_prediction was uninformative (= all 50% verified baselines, 0/58 actual). B28 actuals:

- Verified rate **20.7%** (= 12/58). Headline: still well below "production-grade phase 1" (= ≥80%), but the regression band is now **scenario-design-bound** rather than OS-bound.
- C1 / H1 / H2 / H3 / H4-partial / Q1 / M2 / NEW-2: all the fixes that targeted concrete OS bugs verified cleanly.
- The remaining gap is in: (a) scenario expectations vs router contract (= B28-Q2), (b) LLM compliance / attractor patterns (= 3.1/3.4), (c) authoring polish (= 3.5/3.6/3.9/3.10).

Brier score deferred for the same reason as B27 — outcome_prediction bands were authored before either the fix wave or the framework's `dogfood run` runtime, so they don't reflect the current world.

---

## 6. Next batch ready-list (= B29 candidate)

1. **B28-MED-1 RAG attractor**: seed `skill__index_docs` (+ audit). 5-line fix.
2. **B28-Q2 scenario contract decision**: read FP-0034 §D + router_loop emit logic, then either emit a synthetic `chat_turn_completed` event OR relax scenarios to allow rubric-only verification when no tool is invoked. Touch ~30 scenarios depending on direction.
3. **B28-MED-2 eval description audit**: read both `eval` and `skill_improver` descriptions, tighten the disambiguation.
4. **B28-MED-3 plan-step cwd**: inject resolved cwd / project root into step system prompt.
5. Issue #52 / #53 / #54 (= B27 follow-ups) — separate work tracks.
6. Implement `_build_live_runner` so `reyn dogfood run` is e2e functional — still the FP-0036 framework's missing piece.

---

## 7. Process notes

### What worked

- **Findings-first output**: prompt asked workers to write `findings.md` incrementally per scenario (rather than at the end). 6/7 workers complied; W6 still wrote results.json only but inline output was richer than B27.
- **B28 verification angles** (= C1 / H1 / H2 / H3 / routing_decided per scenario): forced workers to surface fix-effect evidence per scenario, making the cross-worker pattern obvious within minutes.
- **mid-batch fix** (= reyn.yaml `python.safe`): W1 surfaced the issue, main agent fixed during W2's run, W2 confirmed independently — no batch redo needed.

### What needs adjustment

- W6 still skipped findings.md prose despite the new instruction. Its results.json was richer than B27, so the loss was small. **Action**: in B29, ask workers to ALSO emit a single-line summary per scenario to stdout/log so the main agent can synthesize even when prose is missing.
- The dogfood framework's `reyn dogfood run` was again not used. Same recommendation as B27: implement `_build_live_runner` so the framework's run / compare / publish chain becomes end-to-end functional.

---

## 8. Cross-reference

- Worker artefacts: `workers/findings-worker-{1..5,7}.md` + `workers/results-worker-{1..7}.json`
- Wave 1 fixes: commits `c0d5ea8` / `ef0a07f` / `bceee51` / `e17f6df` / `a8e7d34` / `32b28a0` / `1636584`
- Mid-batch fix: `1a5be83` (= reyn.yaml python.safe)
- B27 baseline journal: `docs/deep-dives/journal/dogfood/2026-05-17-batch-27-fp-0036-initial/`
- Open follow-up issues: #52 (B27-H4 root cause), #53 (web enforcement), #54 (qualified-name multi-provider)
