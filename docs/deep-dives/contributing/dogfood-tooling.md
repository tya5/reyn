---
type: contributing
topic: dogfood-tooling
audience: [human, agent]
---

# Dogfood Tooling Reference

Three scripts under `scripts/` consolidate the repetitive patterns that previous sandbox_2 sessions hand-rolled in chat for every B-batch dispatch and every NF attractor investigation. Together they turn the dogfood loop (= dispatch → run → aggregate → investigate → fix) into a script-driven pipeline.

This page is the operator reference for the three tools. The narrative methodology — when to dispatch, when to investigate, how to think about attractors vs noise — stays in [dogfood-discipline.md](dogfood-discipline.md). The tools merely implement what the discipline prescribes.

---

## 1. Why these exist — the cost they remove

Pre-tooling, each B-batch dispatch cost a sandbox_2 session ~60 min of wall-clock and ~500 lines of inline markdown:

- 7 worker prompts × 60-80 lines each (= identical structure, varying only by scenario set + port + past-batch citation)
- Manual past-batch verdict paste from prior aggregate.json
- Per-worker git worktree + `reyn.local.yaml` setup (= 10 bash lines × 7)
- Manual aggregate.json assembly from the 7 `results-worker-N.json` files
- Manual past-batch comparison table construction for the retrospective.md

Each NF attractor investigation cost ~30-60 min: per-variant bash for-loop + hand-rolled Python classifier, sequential subprocess execution, manual N-sample aggregation, manual side-by-side table construction.

After the bundle, both patterns drive from a single YAML config per use case. Per-batch wall drops to ~15 min (= mostly waiting for live `reyn web` startup); per-NF attractor ablation drops to ~10-15 s (= asyncio subprocess fan-out).

---

## 2. Tool catalogue

### 2.1 `scripts/dogfood_variant_replay.py`

**Purpose**: run N-sample × M-variant trace-patch-replay ablations against an existing `REYN_LLM_TRACE_DUMP` trace file. Emits a markdown comparison table with classifier-bucketed counts per variant.

**Use when**: investigating an NF (= attractor / scope-gap / regression candidate). The standard `feedback_verify_fix_via_replay_before_land` discipline requires N=10 ablation before any structural fix lands; this is the runner.

**Inputs**: one YAML config (`trace`, `req_id`, `model`, `n`, optional `parallel`, ordered `classifiers`, list of `variants` each with `patches`).

**Example config** (`variant_ablation.yaml`):

```yaml
trace: /tmp/reyn-worktrees/b43-7/.reyn/llm_trace.jsonl
req_id: 1847830e-8eb0-4056-b129-cc160dc7d3f9
model: gemini/gemini-2.5-flash-lite
n: 10
parallel: 8
classifiers:
  - {label: HALLUCINATE, expr: 'len(content) >= 200 and "consistency" in content.lower()'}
  - {label: ACK,         expr: '0 < len(content) < 300'}
  - {label: EMPTY,       expr: 'not content and not tool_calls'}
  - {label: TOOL_CALL,   expr: 'bool(tool_calls)'}
  - {label: OTHER,       expr: 'True'}    # catch-all
variants:
  - {name: A_bare,        patches: []}
  - {name: D_directive,   patches: ['messages[3].content+=\n\n---\nNow write a short reply...']}
```

**Invocation**:

```bash
python scripts/dogfood_variant_replay.py --config variant_ablation.yaml
```

**Output**: comparison table to stdout.

**Classifier expression language**: Python `eval` with a curated whitelist of safe builtins (`len`, `bool`, `any`, `all`, `isinstance`, `min`, `max`, etc.). Three locals: `content` (str, "" when null), `tool_calls` (list), `finish_reason` (str). First-match-wins ordering. A broken expression falls through to the next rule rather than halting the batch (= catches typos in caller YAML without aborting); an unmatched sample lands in `UNCLASSIFIED` and surfaces an extra table column.

---

### 2.2 `scripts/dogfood_batch_dispatch.py`

**Purpose**: generate the 7 worker markdown prompts a B-batch dispatch needs, with past-batch verdicts auto-cited from prior aggregate.json. Optionally prepare git worktrees + `reyn.local.yaml`.

**Use when**: starting a new B-batch (B44, B45, …). One config drives the entire dispatch flow.

**Inputs**: the shared YAML batch config (see Section 3 below).

**Invocation**:

```bash
# Dry run — print prompts to stdout
python scripts/dogfood_batch_dispatch.py --config batch_b44.yaml

# Write prompts to per-worker files
python scripts/dogfood_batch_dispatch.py --config batch_b44.yaml \
  --prompts-dir /tmp/b44-prompts/

# Also create worktrees + write reyn.local.yaml with flash-lite-only tiers
python scripts/dogfood_batch_dispatch.py --config batch_b44.yaml \
  --prompts-dir /tmp/b44-prompts/ \
  --setup-worktrees \
  --repo-root /Users/.../sandbox_2
```

**Worktree side-effect**: `--setup-worktrees` is opt-in. Each worker's `worktree` path gets a git worktree (idempotent — skipped if the directory already exists) plus a copy of the repo root's `reyn.local.yaml` with `models.strong` rewritten to flash-lite. This enforces `feedback_no_strong_model` at the tool layer rather than relying on operator discipline.

**Past-batch citation**: the prompt template renders a verdict table pulled from the configured `past_batches[].aggregate_path` files. The verdicts the sub-agent worker compares against come from primary data (= committed aggregate.json), not from copy-pasted strings in the prompt.

---

### 2.3 `scripts/dogfood_aggregate.py`

**Purpose**: consume the 7 `results-worker-N.json` files written during a dispatch run + emit `aggregate.json` plus a markdown comparison table for the retrospective.md.

**Use when**: after all 7 workers complete and write their JSON deliverables.

**Inputs**: the same YAML batch config (= journal_dir is read to find worker files; past_batches drive the delta calculations).

**Invocation**:

```bash
# Dry run — print aggregate JSON + table to stdout / stderr
python scripts/dogfood_aggregate.py --config batch_b44.yaml

# Persist aggregate.json to <journal_dir>/aggregate.json
python scripts/dogfood_aggregate.py --config batch_b44.yaml --write
```

**Output shape**: matches the B42 / B43 aggregate.json structure (= `scenarios_total`, `verdict_totals`, `verified_rate`, `env_settings`, per-worker breakdown, `delta_vs_<past>`). The script tolerates the verdict-key divergence between B42 (= long form `verified` / `inconclusive`) and B43 (= short form `V` / `I`) via `_normalise_verdicts`.

**Regression guard**: the published B43 aggregate (= V=22 / I=12 / R=20 / B=0 / scenarios_total=54) is reproduced by `dogfood_aggregate.py` when pointed at the committed B43 journal. The Tier 2 test suite pins this so any future drift in the aggregate shape gets caught.

---

## 3. The shared batch config

Both `dogfood_batch_dispatch.py` and `dogfood_aggregate.py` consume the same YAML schema, defined and validated in `scripts/dogfood_batch_config.py`. One config drives the full B-batch lifecycle.

**Full example** (`batch_b44.yaml`):

```yaml
batch:
  name: B44
  date: 2026-05-21
  head: e96d479f                    # HEAD at dispatch time
  env_vars:
    REYN_EMPTY_STOP_RETRY: "1"      # opt-in retry on empty-stop
    REYN_SPAWN_ACK_TO_LLM: "1"      # opt-in role=tool spawn-ack
  user_params:
    hot_list_n: 10                  # held constant for apples-to-apples
    models_tier: flash-lite
  hard_caps:
    tool_uses: 50                   # feedback_subagent_scope_bounding
    wall_clock_min: 15

workers:
  - {name: W1, scenario_set: chat_router_smoke.yaml,
     scenario_set_path: dogfood/scenarios/chat_router_smoke.yaml,
     port: 8231, n_scenarios: 7,
     worktree: /tmp/reyn-worktrees/b44-1,
     agent_prefix: dogfood-b44-1-s}
  - {name: W2, ..., port: 8232, ...}
  # ... 7 workers total

past_batches:
  - {name: B43, aggregate_path: docs/deep-dives/journal/dogfood/2026-05-20-batch-43-post-empty-stop-retry/aggregate.json}
  - {name: B42, aggregate_path: docs/deep-dives/journal/dogfood/2026-05-19-batch-42-b40-v2-cumulative/aggregate.json}

journal_dir: docs/deep-dives/journal/dogfood/2026-05-21-batch-44-...
```

The schema is intentionally flat: every field is required (or has an explicit default), and missing fields raise `ValueError` at load time so the operator sees a clean error instead of a downstream `KeyError`.

---

## 4. The script-driven dogfood loop

The three tools compose end-to-end. A B-batch from start to finish looks like:

```text
1. Author batch_bN.yaml
       │
       ▼
2. python scripts/dogfood_batch_dispatch.py --config batch_bN.yaml \
       --prompts-dir /tmp/bN-prompts/ --setup-worktrees
       │   (= 7 worker prompts + 7 worktrees ready)
       ▼
3. Spawn 7 sub-agents (= one per worker prompt) — they execute the
   dispatched runs and write results-worker-{1..7}.json
       │
       ▼
4. python scripts/dogfood_aggregate.py --config batch_bN.yaml --write
       │   (= aggregate.json persisted + comparison table to stdout)
       ▼
5. For each NF surfaced in the retrospective:
       python scripts/dogfood_variant_replay.py --config nf_ablation.yaml
       │   (= N=10 × M variants in ~10-15 s)
       ▼
6. Land fix PR (if structural) or close NF (if noise)
```

Steps 1, 2, 4, 5 are now single-command operations. The narrative parts that stay in the operator's head are: scenario set selection (= step 1), per-NF hypothesis formation (= between steps 4 and 5), fix design + landing (= step 6). These are the parts where judgment matters and tooling shouldn't try to substitute.

---

## 5. When to use them, when not to

Use the variant_replay tool whenever you would otherwise run a bash for-loop that calls `scripts/llm_replay.py` multiple times. If you're running N ≥ 3 samples and / or M ≥ 2 variants, the tool is the right shape.

Skip it for single-shot exploratory replays — `scripts/llm_replay.py` directly is faster when N=1 and you just want to inspect one response.

Use the batch tools when launching a new B-batch where the worker assignment matches the standard 7-worker layout. Custom layouts (= e.g. 3-worker mini-batches for targeted ablation) can still use the YAML config — the schema doesn't hard-code the worker count.

Skip the batch tools for one-off scenario runs outside the B-batch cadence — they bring more machinery than a single `reyn agent new` + curl POST round-trip needs.

---

## 6. Extension points

If you need a new classifier label or directive pattern, add it to the YAML — no code change required. If you need to support a new tool surface (= e.g. a new wrapper around `dogfood_trace.py`), follow the same shape: one YAML config, one CLI entry point, one classifier / template / aggregator module, Tier 2 tests against committed data fixtures rather than synthetic JSON. The pattern from these three tools is documented as "design as code" — see `feedback_design_as_code` (= committed memory entry once this lands) for the rationale.

---

## 7. Known limitations under parallel load

These are surface conditions surfaced during the B47 N=10 reproducibility check (2026-05-21). None are bugs in the production single-agent dispatch path; all are specific to the **parallel-dispatch-per-worktree** pattern the tooling uses when batch-running scenarios. They are documented here so operators know what to expect and can choose between (a) tolerating the symptom, (b) reducing parallelism, or (c) carrying out a deeper fix later.

### 7.1 `safe`-mode Python preprocessor timeout under high parallelism

**Symptom**: A skill that uses a `python` preprocessor step in `safe` mode (e.g. `word_stats_demo`'s `stats.py`) intermittently fails with `python step <module>:<function> timed out after <N>s`. At N=10 parallel dispatch of the same scenario, 7 of 10 runs hit this; the skill's trivial counting function should run in microseconds.

**Root cause**: each safe-mode call spawns a fresh Python subprocess that imports the full `reyn` package before running the user function. Measured cold-start of `python -m reyn.kernel._python_harness` is ~2.2 s on a quiet machine. Under 10-way parallel dispatch the subprocesses contend for disk I/O during their parallel imports, and the per-call wall-clock blows past the default `timeout: 5` declared in many skill DSLs.

**Observed rate**: 7/10 timeout at the worst case (N=10 simultaneous, same worktree). Single-agent dispatch never hits this. The 3-7 worker B-batch dispatch (= 1 scenario per worker at a time, scenarios serialised inside a worker) also doesn't reproduce because each worker only ever has one Python step in flight.

**Mitigations**:

- **Tolerate (recommended for B-batches)**: ignore. The standard 7-worker dispatch doesn't trigger this — only when an operator deliberately fires N parallel A2A POSTs against the *same* skill via the *same* worktree (= NF reproducibility checks).
- **Raise the timeout** in the affected skill's `skill.md` `permissions.python.timeout`. Doubling to 10 s usually clears even high-contention cases; the only cost is slower failure for genuinely stuck functions.
- **Reduce parallelism** in the reproducibility check (= N=3 sequential instead of N=10 parallel) — equivalent statistical evidence for binary outcomes at the cost of wall-clock.
- **Deeper fix (deferred)**: pre-warm subprocess pool, in-process restricted execution for safe-mode, or lighter harness that doesn't import full `reyn`. None of these are blocking; documented here as a future architectural option.

### 7.2 `skill_builder` LLM-generated skill name does not preserve user input verbatim

**Symptom**: When the user prompt requests `skill_builder` to create a skill with a specific name (e.g. `"web_summary_repro2"`), the LLM-driven phases (`plan_skill` / `design_artifacts`) occasionally drop or shorten the user-given suffix. In B47 N=10 reproducibility, 9 of 10 runs created `web_summary` or `web_summary_repro` instead of `web_summary_repro2`. Only 1 run preserved the verbatim name.

**Root cause**: this is `flash-lite` (= weak-model) behaviour at the upstream planning phases, not a Reyn OS bug. The build_skill phase faithfully writes the name decided by `plan_skill` / `design_artifacts`; the LLM at those phases interprets the user's name as a description-with-suffix and "tidies" it. Strong tier (`gemini-2.5-flash`) does not exhibit the same hallucination.

**Observed rate**: 9/10 at flash-lite under parallel load. Single-agent dispatch at flash-lite still exhibits the same behaviour (= not parallelism-specific, but more visible at parallel scale).

**Mitigations**:

- **Tolerate** in dogfood B-batches where the scenario rubric checks "*a* skill was created", not "*this exact name* was created". The B-batch rubrics for `skill_builder_web_summariser` (= W2-S4) already accept any successful build.
- **Use strong tier for `plan_skill` / `design_artifacts`** if name fidelity matters. The default keeps them at standard because most use cases tolerate the shortening; production setups that require name preservation should upgrade.
- **Don't fix at the OS layer**. Per `feedback_reyn_care_boundary`, this falls under "LLM への注文" (= LLM compliance to prompt), where Reyn's structural intervention surface is minimal.

### 7.3 Router catalog fuzzy-match on parallel-built skills

**Symptom**: When several `skill_builder` invocations run in parallel within the same worktree, the second-and-later builds may not dispatch `skill_builder` at all — instead, the router LLM picks the *already-built* skill from a previous parallel run (= because the previous build's `reyn/local/<name>/skill.md` made it discoverable in the catalog hot-list) and invokes it directly. The user's intent to "build a new skill" is lost because the catalog reports a fuzzy-matching skill is already available.

**Root cause**: shared filesystem state (= `reyn/local/`) between parallel agents in the same worktree. The first parallel build wins; subsequent calls see the catalog and route to the existing skill. No race in the build itself — just LLM routing prior contaminated by mid-batch catalog growth.

**Observed rate**: 3/10 at N=10 parallel `skill_builder` requests in the same worktree (B47 reproducibility). Sub-batches (= 1 worker per scenario, scenarios serialised) don't reproduce because each worker has its own worktree and the catalog is fixed for the worker's lifetime.

**Mitigations**:

- **Per-worker worktree (the standard B-batch pattern)**: prevents catalog contamination entirely. The `--setup-worktrees` flag in `dogfood_batch_dispatch.py` is exactly this defence.
- **Distinct skill names per parallel run**: if multiple parallel dispatches must share a worktree, give each scenario a unique target skill name so the LLM cannot fuzzy-match.
- **Tolerate** for measurement runs that fold "no-build, invoked existing skill" into the FAIL bucket — the verdict is still informative about LLM routing priors.

### 7.4 Cross-cutting observation

All three limitations share the same shape: they fall under `feedback_reyn_care_boundary`'s "LLM への注文" (= LLM compliance) or "structural environment" boundary, where the OS has limited or zero direct intervention surface. They reproduce reliably under high-parallelism stress (= N=10 same-worktree) but disappear under the production-style dispatch the tooling itself recommends (= 1 worker per worktree per scenario). They were surfaced **by the increased parallelism of the N=10 reproducibility check**, which is also operating outside the tools' designed comfort zone.

The lesson is operational: when an N=10 reproducibility check shows a failure mode that wasn't in the original B-batch, check whether it's parallelism-specific before treating it as a real regression. The B47 retrospective documents this for `#337` (= stats=0 hypothesis) and `#358` (= invalid-JSON hypothesis) — both turned out to be N=1 noise that the N=10 check disambiguated.

---

## 8. Related references

- `docs/deep-dives/contributing/dogfood-discipline.md` — when to dispatch, what to measure, why.
- `docs/deep-dives/contributing/testing.md` (or `.ja.md`) — the testing policy these tools comply with (= no `unittest.mock.patch`, real fixtures, Tier-tagged docstrings).
- `scripts/llm_replay.py` — the underlying replay CLI the variant runner wraps.
- `feedback_subagent_scope_bounding` — the hard-caps discipline the batch dispatch template propagates.
- `feedback_no_strong_model` — the flash-lite-only constraint `--setup-worktrees` enforces.
- `feedback_verify_fix_via_replay_before_land` — the discipline the variant_replay tool implements at runtime.
