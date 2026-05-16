# Batch 27 — Retrospective

> First batch using the FP-0036 framework's 58-scenario starter set; first
> batch after `feedback_dogfood_parallel_reyn_agent_isolation.md` + 9-principle
> framework formalisation. Headline: **0/58 verified**, but the regression is
> diagnostic — three real structural issues + one CRITICAL infra bug surfaced
> cleanly, and the parallel-7 worker pattern held.

---

## 1. What this batch verified, what it didn't

### Verified (= the framework + workflow worked)

- **7-sonnet parallel dispatch with worktree + per-reyn-agent isolation
  held**. Zero state collision across 7 concurrent sessions, total wall-clock
  ~12 min. Memory `feedback_dogfood_parallel_reyn_agent_isolation.md` was
  honoured cleanly.
- **Trace observation infra is sufficient**. `REYN_LLM_TRACE_DUMP` +
  `scripts/dogfood_trace.py` + per-agent `.reyn/events/<agent>/*.jsonl`
  produced primary-data evidence for every finding. No finding rests on
  speculation; this is the discipline from
  `feedback_observe_before_speculate_llm.md` being applied at batch level
  for the first time at this parallel scale.
- **`feedback_pre_conclusion_observation_checklist.md` discipline held**.
  No worker wrote "100%" or "decisive" without primary-data backing. W3
  explicitly produced a fix location with line numbers; W6 source-verified
  via reading `router_tools.py:888`.

### Not verified (= things this batch cannot conclude about)

- **The FP-0036 framework's `reyn dogfood run` end-to-end path** — workers
  used the legacy `reyn chat` stdin pipe pattern (= same as
  `dogfood_b24_driver.py`) because the framework's live runner is still a
  stub. The framework's `run` subcommand was not exercised.
- **#40 fix effectiveness** — plan tool is hidden from the LLM via
  `_LEGACY_TOOL_NAMES`, so the updated `_PLAN_PARAMETERS` text never
  reached an LLM call. Verification is blocked behind B27-H1.
- **Regression timing** — the duplicate-declaration bug almost certainly
  predates this batch, but the exact commit window is not pinned. Batch 26
  (= 91.4% verified) ran a different scenario set with the same router
  code; need to diff `scripts/dogfood_b24_driver.py`'s seven scenarios
  against the FP-0036 starter set to see whether B26 simply didn't trigger
  the bug, or whether something landed between
  `dd28502` (B26 HEAD) and `5965b58` that changed alias-builder behaviour.

---

## 2. What changed since the last batch and how those changes mapped to findings

### Changes between batch 26 (`dd28502`, 2026-05-16) and batch 27 (`5965b58`, 2026-05-17)

| commit | nature | impact this batch |
|---|---|---|
| `cf6dde2..afded90` | FP-0036 framework + 58 scenarios + reporting + automation | scenario set authored, ran |
| `1c53a6d` | #40 fix (plan.py) | bypassed — see B27-H1 |
| `972dde4` | #49 fix (universal_catalog visibility) | half-shipped — see B27-H2 |
| `5965b58` | plan_mode scenarios | exercised, but blocked |

The FP-0036 framework code itself did not break anything observable in B27.
The scenario set surfaced bugs that existed in `main` already.

### Re-emergence of the duplicate-declaration class

Memory `feedback_envelope_layer_fix.md` documents a prior envelope-layer
attractor fix (Pattern E = post-tool empty-stop). The duplicate-declaration
bug is **architecturally similar** — a protocol-level (= function-list)
violation that LLM behaviour can amplify but the OS layer must prevent.
The fix layer is again envelope (= `_build_hot_list_aliases` filter), not
SP content. This matches the prediction in
`feedback_envelope_layer_fix.md` that "when the surface is repeatedly
different but the symptom converges, look at the envelope."

---

## 3. Process reflection

### What we want to keep

1. **Dispatch prompt template structure** (= environment, scenario source,
   execution recipe, discipline gates, trace tools, output contract,
   boundaries, report line). Every worker produced parseable
   `results.json` even when it skipped the prose markdown.
2. **Per-worker LLM trace dump as primary evidence channel**. Every HIGH
   finding has a trace excerpt — re-checkable, not narrative.
3. **The 4-band verdict (verified/inconclusive/refuted/blocked)** caught
   the duplicate-declaration bug correctly as `blocked` not `refuted`,
   which preserved the calibration distinction between "couldn't observe"
   and "observed contradiction".

### What needs adjustment

1. **3/7 workers (W4, W6, W7) wrote results.json but not findings.md**.
   Same prompt shape was given to all 7, so the cause is probably token
   budget at the end of a long sub-agent transcript. **Action**: in the
   next dispatch, ask the worker to write findings.md FIRST and results.json
   second, so if the budget runs out the prose survives.
2. **The FP-0036 framework's `run` subcommand was not used**. This was the
   nominal point of the batch. **Action**: implement
   `_build_live_runner(agent_name)` in
   `src/reyn/cli/commands/dogfood.py:335` so it actually drives a
   headless chat session and returns a `ScenarioRunResult`. Reuse the
   `send_to_agent_impl` path (= `src/reyn/mcp_server.py:167`) rather than
   shelling out to `reyn chat`. Until that lands, dogfood batches keep
   running through the legacy driver pattern and the framework's
   `compare` / `publish` / `coverage` subcommands have no real upstream.
3. **The starter set's `outcome_prediction` bands need recalibration** —
   they were authored under assumptions that the live runner would
   exist and that `skill_run_spawned` would emit on every chat turn.
   Predictions should be deferred (or set to a calibrated neutral) until
   B27 fixes land and we have one good batch to learn from.

### What surprised us

- **The hot-list aliasing mechanism interacts globally** — once any
  session calls a universal wrapper, every subsequent fresh agent on the
  same `.reyn/state/action_usage.jsonl` inherits the duplicate. This is
  the "state crosses session boundaries silently" failure mode that
  memory `project_local_env.md` and the workspace P5 doc both warn
  about, but neither named hot-list aliases specifically. The blast
  radius (= 13 blocked + bleeds into refuted) is larger than any single
  bug we've previously hit in dogfood.
- **The `web__fetch` enforcement gap** is a clean example of "fixing the
  visible symptom (= list_actions output) without fixing the underlying
  enforcement". Memory `feedback_verify_reproduce_first.md` warned about
  exactly this with the term "wrong layer trap". My #49 fix targeted the
  visibility layer; the enforcement layer was already gone since FP-0022.
  This is on me, not the framework.

---

## 4. What I would tell the next batch (= operational learnings)

1. **Read `feedback_envelope_layer_fix.md` before designing protocol-level
   fixes**. The B27-C1 bug is an envelope-layer issue (= tool-list shape);
   any SP edit attempting to "guide the LLM around it" would fail. The
   fix layer ladder (envelope → schema → SP content) is again decisive.
2. **`reyn chat --cui <agent>` with stdin pipe is the working driver for
   dogfood today**. The FP-0036 framework's `dogfood run` is not yet
   wired to a live runner. If you need to run a batch right now, use the
   `dogfood_b24_driver.py` pattern.
3. **Hot-list state survives across sessions** — wipe
   `.reyn/state/action_usage.jsonl` between scenarios if you need clean
   isolation. Or fix B27-C1 first.
4. **Scenario authors should source the expectation list from a router
   contract document, not from "what they think happens"**. The
   `skill_run_spawned` mismatch (B27-Q1) is the canonical case: 4
   workers asserted the same wrong expectation because the scenario
   yaml said so. Until there is a single source of truth for "what
   events does the router emit during an inline op", scenarios will
   keep drifting.

---

## 5. Fix wave priorities (= ready to dispatch)

1. **B27-C1** — `_build_hot_list_aliases` filter for universal wrapper
   names. Probably 5 lines + 1 test. Highest leverage, smallest
   implementation. Re-run W7 long-session set to validate.
2. **B27-H1** — remove `"plan"` from `_LEGACY_TOOL_NAMES`. Probably 1
   line + 1 test. Re-run W6 plan_mode set to validate.
3. **B27-H2** — restore enforcement gate for direct `web__fetch` /
   `web__search` router-tool exposure under config-deny. Probably 5-10
   lines in `build_tools()`. Re-run W4 S8 to validate.
4. **B27-Q1 design decision** — read ADR-0034 + FP-0001 router-vs-skill
   contract, then decide whether to (a) emit `chat_turn_spawned` or (b)
   relax scenarios. Do not dispatch a fix until the design is settled.
5. **B27-H3 / H4 / M-series** — defer to next wave; not blocking.

After fix wave: implement `_build_live_runner` (= the missing FP-0036
piece) and re-run a sanity subset under the framework's actual `run`
subcommand. That will be batch 28's headline.

---

## 6. Cross-reference

- Findings: `findings.md` (this directory)
- Per-worker artefacts: `workers/findings-worker-{1,2,3,5}.md` +
  `workers/results-worker-{1..7}.json`
- Source of bug B27-C1: `src/reyn/chat/router_loop.py:357` + `:565`
- Source of bug B27-H1: `src/reyn/chat/router_tools.py:888`
- Source of bug B27-H2: `src/reyn/chat/router_tools.py:538` (gate removed)
  + `src/reyn/tools/universal_catalog.py` (visibility-only fix)
- Memory: `feedback_envelope_layer_fix.md`,
  `feedback_observe_before_speculate_llm.md`,
  `feedback_pre_conclusion_observation_checklist.md`,
  `feedback_dogfood_parallel_reyn_agent_isolation.md`,
  `feedback_verify_reproduce_first.md`,
  `project_dogfood_findings.md`.
