# Batch 27 — Findings (FP-0036 framework initial e2e)

> **First large-scale dogfood batch using the FP-0036 scenario framework's
> 58-scenario starter set.** 7 sonnet workers ran 7 scenario sets (+ plan_mode
> set, + 2 fp_0011_* legacy sets) in parallel worktrees against `main` HEAD
> `5965b58`. Result: **0/58 verified (0.0%)** — a hard contrast against batch 26
> (= 91.4% verified). The headline is **not** "Reyn is broken" — it is
> "scenario-design expectations and current router behaviour diverged in
> three independent ways, and one CRITICAL infra bug compounded everything."

---

## 0. Run summary

| Item | Value |
|---|---|
| Branch HEAD | `5965b58 feat(fp-0036): plan-mode dogfood scenarios` |
| Tests (pre-batch) | 3315 passed / 5 skipped / 2 xfailed |
| Total scenarios planned | 58 (= 8 yaml sets) |
| Workers | 7 sonnet parallel (= per-cwd + per-reyn-agent isolation) |
| Worktrees | `/tmp/reyn-worktrees/b27-{1..7}` (branches `dogfood/b27-{1..7}`) |
| Wall-clock | ~12 min (= longest worker 12m02s) |
| LLM model | `gemini-2.5-flash-lite` via local LiteLLM proxy |
| Driver pattern | per-worker: `reyn chat --cui <agent>` stdin pipe + `REYN_LLM_TRACE_DUMP` |
| Aggregate verdict | **V=0 / I=19 / R=26 / B=13** |
| Verified rate | **0.0%** |

---

## 1. Per-worker verdict matrix

| Worker | Scenario set | Count | V | I | R | B |
|---|---|---|---|---|---|---|
| 1 | chat_router_smoke | 7 | 0 | 0 | 3 | 4 |
| 2 | stdlib_skills_core | 9 | 0 | 2 | 6 | 1 |
| 3 | control_ir_ops | 9 | 0 | 3 | 3 | 3 |
| 4 | permissions_and_safety | 8 | 0 | 7 | 1 | 0 |
| 5 | multi_agent_and_mcp | 7 | 0 | 1 | 6 | 0 |
| 6 | plan_mode + fp_0011_* | 11 | 0 | 6 | 0 | 5 |
| 7 | long_session_v1 | 7 | 0 | 0 | 7 | 0 |
| **Total** | — | **58** | **0** | **19** | **26** | **13** |

Per-worker findings under `workers/findings-worker-{1,2,3,5}.md` (4 workers wrote
markdown). Workers 4, 6, 7 wrote `results.json` only; their detailed prose
findings are summarised here from the dispatch result notifications.

---

## 2. Cross-worker dominant patterns

The verified-rate of 0/58 has **three structural causes** that overlap with
1 CRITICAL infra bug. Counts below are the number of workers that
independently surfaced the pattern.

### 2.1 CRITICAL — Universal-wrapper duplicate function declaration (6/7 workers)

The hot-list builder (`_build_hot_list_aliases` in
`src/reyn/chat/router_loop.py:357`) re-adds direct aliases for universal
wrapper tools (`list_actions`, `describe_action`, `invoke_action`,
`search_actions`) onto the tools array. These wrappers are *already* added
once by section I of `build_tools()`. On any session that follows one where
the LLM called `list_actions` (or another wrapper), the request payload
contains the wrapper at two positions, and Gemini rejects with:

```
GeminiException BadRequestError:
INVALID_ARGUMENT: Duplicate function declaration found: list_actions
```

**Primary evidence (W7 trace `scenario_2.jsonl`)**:
`dupes={'list_actions': 2}` — tools array contains `list_actions` at two
positions (universal wrapper + hot-list alias).

**Trigger**: `.reyn/state/action_usage.jsonl` records `list_actions` /
`describe_action` usage. `ActionUsageTracker.get_top_n()` returns it as
top-ranked, and `_build_hot_list_aliases()` builds an alias without
filtering out wrapper names.

**Workers reproducing independently**: 1, 2 (describe_action variant), 3, 4,
6, 7 = **6/7**.

**Fix location (W3 identified)**: `_build_hot_list_aliases()` in
`src/reyn/chat/router_loop.py` — filter universal wrapper names
(`list_actions`, `describe_action`, `invoke_action`, `search_actions`) from
the `get_top_n()` results or from the alias-builder inputs.

**Blast radius**: 13/58 `blocked` verdicts + an unknown subset of the 26
`refuted` verdicts (W7 marks all 7 long-session scenarios refuted because
turns 2+ each hit this).

### 2.2 HIGH — `plan` tool fully hidden from LLM (W6)

`src/reyn/chat/router_tools.py:888` includes `"plan"` in `_LEGACY_TOOL_NAMES`.
With `universal_wrappers_enabled=True` (= default), `plan` is stripped from
the LLM's tool list. The LLM never sees `plan` and never invokes it.

**Primary evidence (W6)**: in `plan_compare_two_concepts` scenario the LLM
hallucinated `invoke_action(action_name="default_api.plan")` — a non-existent
action name. No `plan_emitted` event fired in any of the 3 plan_mode runs.

**Source-verified**: `router_tools.py` line 875–888.

**Impact on #40 fix**: the `_PLAN_PARAMETERS` text overhaul (commit `1c53a6d`)
is installed correctly but never reaches the LLM. **#40 fix verification
status: inconclusive** (= cannot e2e verify without first fixing this).

### 2.3 HIGH — `web__fetch` direct expose (W4: #49 fix incomplete)

#49 fix (commit `972dde4`) hid `web__fetch` from `list_actions` output when
`web.fetch: deny` is configured. But `web__fetch` is *also* exposed as a
direct router tool via section E of `build_tools()`. Per FP-0022,
`web_fetch_allowed` gate was removed (`router_tools.py` line 538: "removed
catalog-level gate... parameter kept for backward compat but ignored").

**Primary evidence (W4 trace `web_fetch_denied_by_config`)**: request
`4f690802` tools list contains `web__fetch`; `invoke_action(web__fetch, ...)`
returned `status: ok, status_code: 200` with real network content. No
`permission_denied` event emitted despite `web.fetch: deny` in
`reyn.local.yaml`.

**Implication**: the visibility-layer fix (#49) needs an enforcement-layer
companion to actually block the call. Either re-introduce the `web_fetch_allowed`
gate, or remove direct exposure of `web__fetch` from the router-tool list
when universal wrappers are enabled.

### 2.4 HIGH — Scenario expectation drift (W1/W3/W4/W7)

7 of 8 scenarios in `permissions_and_safety.yaml` and most others assert
`must_emit: skill_run_spawned`. The chat router dispatches permission-gated
ops directly via `invoke_action → op handler` without spawning a skill run.
This is consistent across W1/W3/W4/W7 = at least 4 independent traces.

**Open question**: is the design intent that every chat turn spawns a skill,
or is the scenario expectation overly strict? This is a **scenario-design
vs OS-design** judgement, not an immediately fixable bug. Resolution path:
read the FP-0001/FP-0034 ADRs for the intended router-vs-skill split and
update either the scenarios or the router event-emission contract.

### 2.5 MED — Per-worker localised bugs

| Worker | Bug | Evidence |
|---|---|---|
| 2 | `skill_run_completed` → `skill_run_interrupted` (S2, S4); coroutine warning `OpenAIChatCompletion.acompletion was never awaited` | trace + events log |
| 5 | `KeyError: 'request'` in peer-agent `invoke_action` handler (S3, S4) | error trace |
| 5 | `mcp_search` requires `--allow-unsafe-python` flag (S1) | preprocessor error |
| 7 | LLM hallucinates `reyn__source__read` (= double-underscore as category sep) instead of `reyn.source__read` | scenario_1 turn-3+ tool_failed events |
| 1 | `routing_decided` event (FP-0034 Phase 6) never emitted in any chat turn | 3/3 non-blocked event files inspected directly |
| 3 | `file__grep` has no routing rule despite appearing in `DEFAULT_HOT_LIST_SEED` | S2 tool_failed trace |
| 3 | `exec__run` (non-existent) called by LLM instead of `exec__sandboxed_exec`; no retry | S5 trace |

---

## 3. Severity classification (= dogfood-discipline §A5)

### CRITICAL — system non-functional

- **B27-C1**: Universal-wrapper duplicate declaration (`_build_hot_list_aliases`
  needs wrapper-name filter). Reproduced by 6/7 workers. **Fix wave priority 1.**

### HIGH — core user path blocked

- **B27-H1**: `plan` tool fully hidden by `_LEGACY_TOOL_NAMES`. Plan-mode is
  non-functional under `universal_wrappers_enabled=True` (= the default).
- **B27-H2**: `web__fetch` direct expose despite `web.fetch: deny` (= #49 fix
  is visibility-only; enforcement layer missing).
- **B27-H3**: peer-agent `invoke_action` raises `KeyError: 'request'`.
- **B27-H4**: `skill_run_completed` lifecycle: ends at `_interrupted` with
  `acompletion was never awaited` coroutine warning.

### MED — degraded behaviour with workaround

- **B27-M1**: `routing_decided` event missing — audit completeness gap, not
  a runtime blocker.
- **B27-M2**: `file__grep` routing rule absent.
- **B27-M3**: `mcp_search` unsafe-python flag onboarding gap.
- **B27-M4**: `reyn__source__read` action-name format confusion in LLM output.
- **B27-M5**: `list_actions(filter=path)` misuse for directory listing
  (W2) — description ambiguity, not a runtime bug.

### Open question (= not a fix decision yet)

- **B27-Q1**: `skill_run_spawned` expectation drift. Scenarios assume every
  chat turn spawns a skill; current router doesn't. **Pre-fix: read the
  router-vs-skill design intent** before deciding which side to change.

---

## 4. Calibration (= prediction vs actual)

The starter set's `outcome_prediction` bands were authored under the
assumption that the framework's runner would drive scenarios deterministically
and that the router would behave as documented. Both assumptions broke:

- **0/58 verified** vs predicted average ~50% verified — **~50 pp gap** across
  the board.
- Predictions did not account for the duplicate-declaration bug (= it
  rolled in only after B23-26 wave commits + the recent hot-list seeding
  changes).
- `skill_run_spawned` expectations assumed skill-lifecycle dispatch for chat
  turns — the actual router architecture under FP-0034 Phase 1 does not
  spawn skills for inline ops.

Brier score against these predictions would be uninformative until the
3 HIGH bugs are fixed and the scenario design is reconciled with the
router contract. Skipping Brier this batch.

---

## 5. Blockers (= dogfood-discipline §A4 sense-check)

- **B27-C1** (= duplicate declaration) blocks an estimated 13 scenarios
  directly (= the `blocked` verdicts) and contaminates an additional unknown
  subset of `refuted` verdicts where the first 1-2 turns succeeded then
  the bug kicked in mid-scenario (= W7 scenario_7 survived 4/5 turns).
- **B27-H1** (= plan strip) blocks the entire `plan_mode.yaml` set (3
  scenarios).
- **B27-Q1** (= skill_run expectation) inflates the `refuted` count by an
  unknown amount; recalibration may move some refuted → verified.

---

## 6. Trace artifact pointers

- Per-worker findings markdown: `workers/findings-worker-{1,2,3,5}.md`
- Per-worker `results.json` (= all 7 present): `workers/results-worker-{1..7}.json`
- Per-scenario LLM trace dumps: `/tmp/reyn-worktrees/b27-<idx>/traces/<scenario_id>.jsonl`
- Per-scenario event logs: `/tmp/reyn-worktrees/b27-<idx>/.reyn/events/<agent>/*.jsonl`

These survive only as long as the worktrees do. The aggregate
`results-worker-*.json` files copied into this journal directory are the
durable artifacts.

---

## 7. Next batch ready-list (= fix wave candidates)

In priority order, drawn from §3:

1. **B27-C1 fix** — filter wrappers from hot-list aliases (= 5-line patch in
   `_build_hot_list_aliases`). Single highest-leverage change. Validation
   path: re-run worker 7's long-session set, expect >=5/7 to complete all turns.
2. **B27-H1 fix** — remove `"plan"` from `_LEGACY_TOOL_NAMES`, or expose
   `plan` as a tool unconditionally. Validation: re-run worker 6's plan_mode
   set, expect `plan_emitted` in >=2/3 scenarios.
3. **B27-H2 fix** — add enforcement-layer gate for `web__fetch` /
   `web__search` direct expose under config-deny. Validation: re-run worker
   4's S8, expect `permission_denied` event.
4. **B27-Q1 design reconciliation** — read ADR-0034 / FP-0001 / FP-0034 §D
   for router-vs-skill dispatch intent, then either (a) emit a
   `chat_turn_spawned` synthetic skill_run, or (b) update scenarios to
   match the inline-op router path. **No fix dispatched until decision
   is made — this is a spec-vs-bug judgement.**
5. **B27-H3 / B27-H4** — peer-agent KeyError and skill_run_interrupted
   bugs need direct source debugging; defer to next wave once routing
   path is restored.

---

## 8. Process notes (= retrospective material)

- **7 sonnet parallel dispatch worked** — no worker collided on state,
  worktree isolation held cleanly. Total wall-clock ~12 min.
- **3 of 7 workers skipped the findings.md output** (= W4, W6, W7). They
  wrote `results.json` correctly. Cause analysis pending — prompt was
  identical in shape across all 7. May reflect time pressure near end of
  long traces. **Action**: tighten the per-worker output checklist in the
  dispatch prompt, or split findings.md into a deterministic template.
- **`reyn dogfood` framework was NOT used** for execution — the runner is
  still the FP-0036 MVP stub returning `inconclusive`. Workers ran
  scenarios via direct `reyn chat` stdin pipe (= same pattern as
  `scripts/dogfood_b24_driver.py`). **Action**: implement
  `_build_live_runner` in `src/reyn/cli/commands/dogfood.py` so the
  framework's `run` subcommand is end-to-end functional. This was the
  intended deliverable of B27 but the bugs took precedence.
- **Trace tools held up well** — `scripts/dogfood_trace.py` modes
  (`llm-payloads`, `llm-detail`, `llm-tools-schema`) were consumed by 4/7
  workers to surface primary evidence. The `--mode chain` path was used
  by W5 for multi-agent verification.
