---
type: contributing
topic: dogfood-regression-playbook
audience: [human, agent]
---

# Dogfood regression playbook

A step-by-step procedure for running the FP-0036 dogfood scenario suite as a
regression check across Reyn releases. Sister to:

- `dogfood-discipline.md` — methodology layer (= 9 principles, A1-A5 loop)
- `dogfood-reporting.md` — reporting layer (= journal + Discussions + Issues)

This playbook is the **operational layer**: what commands to run, in what order,
and what to decide at each gate.

---

## When to run the regression suite

Run the full scenario suite in the following situations:

- **Before tagging a release** — any version tag that reaches users.
- **After landing any PR that touches OS layer code** — `src/reyn/core/op_runtime/`,
  `src/reyn/core/kernel/`, `src/reyn/chat/`. Structural OS changes can silently break
  routing behaviour that only the scenario suite exercises end-to-end.
- **After landing any PR that touches stdlib skill prompt content** — mutations
  to `skill.md` or phase `instructions` fields affect LLM routing and output
  in ways unit tests do not catch.
- **Quarterly** — full coverage run plus recalibration of all `outcome_prediction`
  distributions in the scenario YAMLs.

Smoke-only runs (= `--n 1` on `chat_router_smoke.yaml` only) are acceptable after
low-risk PRs (documentation, config schema, tooling). Stability runs (`--n 5`)
are required for any OS or prompt change.

---

## Step 0 — Pre-flight checklist

**Purpose**: confirm the test environment is isolated and fully operational before
any LLM cost is spent.

```bash
# 1. Record the SUT commit hash — include in all reports and issue titles.
git rev-parse HEAD

# 2. Confirm Python dependencies are importable.
pip list | grep -E "croniter|httpx|litellm"

# 3. Confirm LiteLLM proxy is running on localhost:4000.
curl -s localhost:4000/v1/models | python3 -m json.tool | head -20

# 4. Create an isolated working directory for this batch.
mkdir -p /tmp/reyn-b<N>
cd /tmp/reyn-b<N>
reyn init          # creates a fresh .reyn/ state directory

# 5. Verify isolation is clean (no leftover sessions).
ls .reyn/           # should contain only init artefacts, no prior runs
```

**Isolation rules** (see memory `feedback_dogfood_parallel_reyn_agent_isolation.md`):

- Each regression batch gets its own `/tmp/reyn-b<N>/` cwd with a fresh `.reyn/`
  state. Do not reuse a development cwd — session state leaks across runs.
- When running parallel scenarios in sub-agents: each sub-agent uses a different
  `--agent-name` so its sessions do not collide with the development agent or
  with other parallel sub-agents.
- Use `--storage /tmp/reyn-b<N>/.reyn/dogfood/runs/` explicitly so all run artefacts
  land in the isolated directory, not in the development workspace.

Do not proceed past Step 0 if any check fails. Fix the environment first.

---

## Step 1 — Record a baseline (first-time setup or new release tag)

A baseline is a named snapshot of a run that future candidate runs are measured
against. Record a baseline once per release cycle, or whenever a known-good state
needs to be preserved.

```bash
# Run the smoke suite with stability count.
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --n 5 \
    --agent default \
    --storage /tmp/reyn-b<N>/.reyn/dogfood/runs/

# The run prints a run_id (UUID). Tag it as a baseline.
reyn dogfood baseline <run_id> --label v0.X-baseline
```

Repeat for every scenario set you want to track:

```bash
reyn dogfood run dogfood/scenarios/stdlib_skills_core.yaml --n 5
reyn dogfood baseline <run_id> --label v0.X-stdlib-baseline

reyn dogfood run dogfood/scenarios/permissions_and_safety.yaml --n 5
reyn dogfood baseline <run_id> --label v0.X-permissions-baseline
```

**Baseline label convention**: encode the SUT version and the set name so the
label is unambiguous when comparing months later.

| Pattern | Example |
|---|---|
| Release tag | `v0.3-chat-router` |
| Quarterly | `2026-Q2-stdlib` |
| Pre-merge gate | `pre-pr-42-permissions` |

Baselines live under `.reyn/dogfood/baselines/<label>/` as symlinks to the run
directory. They are per-developer (not shared via git) in the MVP; revisit when
CI needs shared baselines.

---

## Step 2 — Candidate run (the regression measurement)

A candidate run is a new run of the same scenario set, executed after a code
change, to measure whether behaviour has changed relative to the baseline.

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --n 5 \
    --agent default \
    --storage /tmp/reyn-b<N>/.reyn/dogfood/runs/
```

`--n 5` is the recommended stability shot count: it matches the production-grade
milestone threshold established in batch 14 (N=5, ≥80% verified). Use lower N
for fast smoke checks; raise to `--n 10` for high-stakes release gates.

After the run completes, note the candidate `run_id` from the output — you need
it in Step 4.

**If running multiple scenario sets**: run them sequentially (or in parallel
sub-agents, each with a separate `--agent-name`). Record all candidate `run_id`s.

---

## Step 3 — Replay mode (optional, zero-LLM-cost)

If fixtures already exist under `dogfood/fixtures/<set>/` (recorded on a prior
run), replay mode re-runs verification without making any LLM calls. Use this
for CI gating where determinism matters and LLM cost is prohibitive.

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml \
    --replay dogfood/fixtures/chat_router_smoke/
```

Replay runs are tagged in the `run_id` so reports distinguish them from live
runs. Do not compare a replay run against a live baseline — the output
distributions are not directly comparable.

**Fixture freshness**: re-record fixtures on every minor release. A schema
mismatch between the fixture and the current runtime forces automatic
re-recording. To re-record manually:

```bash
# Live run that records fixtures (first run after a release):
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5
# Fixtures land in dogfood/fixtures/chat_router_smoke/ automatically.
```

---

## Step 4 — Compare to baseline

```bash
reyn dogfood compare <baseline_label_or_run_id> <candidate_run_id> \
    --threshold 0.05
```

**Exit codes**:

| Code | Meaning |
|---|---|
| `0` | No regression — verified-rate drop is within the 5 pp threshold. |
| `1` | Regression alert — verified-rate dropped more than `--threshold`. |
| `2` | Error — one or both run directories were not found. |

**Reading the comparison output**:

```
  Baseline:  a1b2c3d4-...  (80.0% verified)
  Candidate: b2c3d4e5-...  (46.7% verified)
  Delta:     -33.3pp  /  threshold=-5.0pp
  Result:    REGRESSION ALERT

Regressed scenarios (2):
  - explicit_skill_invocation_word_stats: verified → refuted
  - catalog_routing_decided_emitted: inconclusive → refuted
```

Fields to triage:

- `regressed_scenarios` — each scenario whose worst-case outcome worsened. These
  enter Step 5 triage.
- `improved_scenarios` — scenarios whose outcome improved. Review for
  unexpected side-effects; confirm intentional.
- `verified_rate_delta` — aggregate change. A delta within ±5 pp is noise;
  beyond −5 pp is a regression gate failure.

For JSON output (CI integration):

```bash
reyn dogfood compare <baseline> <candidate> --json; echo "exit: $?"
```

---

## Step 5 — Triage regressed scenarios

For each scenario listed in `regressed_scenarios`, classify it as one of four
triage categories before taking action.

### Category 1: True regression

The SUT changed in a way that broke observable behaviour. The scenario is
faithfully measuring what it was designed to measure; the measurement is now
failing.

**Signal**: The scenario's `expected.events` or `expected.artifacts` block is
refuted — structural verifiers failed, not only the LLM judge.

**Action**:
1. Open a `dogfood-finding` Issue (see `dogfood-reporting.md` for template).
2. Assign severity: CRITICAL / HIGH / MED / LOW using the taxonomy from
   `dogfood-discipline.md` Section A4.
3. Block the release if severity is HIGH+, unless the regression is explicitly
   accepted with a rationale comment in the Issue.
4. After the fix lands, run a post-fix candidate and verify the regression is
   gone (see Step 8).

### Category 2: Scenario flake

The scenario's `expected.*` assertion is too tight relative to LLM stochastic
variation. The underlying behaviour has not changed; the rubric is over-specific.

**Signal**: The outcome flips between `verified` and `refuted` across N=5 runs
with no code change. The `judge` verifier is the only failing verifier (events
and artifacts pass).

**Action**:
1. Loosen the rubric in the scenario YAML — broaden the phrasing, reduce the
   number of criteria, or widen the substring/regex match.
2. Commit the YAML change with a rationale in the commit message:
   `fix(dogfood): loosen rubric for <scenario_id> — original too tight for model variance`.
3. Re-run the candidate against the updated YAML and confirm the flake resolves.

### Category 3: Calibration drift

The scenario outcome is `verified` (behaviour is correct) but the
`outcome_prediction` distribution no longer matches the observed distribution.
Brier score sustains above 0.5 across N≥5 runs.

**Signal**: `summary.json` shows high Brier score for the scenario but outcome
is `verified` or `inconclusive`, not `refuted`.

**Action**:
1. Update `outcome_prediction` in the scenario YAML to match the observed
   distribution.
2. Commit with: `chore(dogfood): recalibrate outcome_prediction for <scenario_id>`.
3. This is a normal quarterly maintenance action; do not open a bug Issue.

### Category 4: Environment dependency

The outcome is `blocked` because a precondition was absent in the operator's
environment — an MCP server was not configured, the web server was not running,
or a permission tier was not granted.

**Signal**: The `blocked` outcome appears in `events.jsonl` as a timeout or
`permission_denied` event. The scenario YAML's `covers:` tags reference an
environment-dependent feature.

**Action**:
1. Note the missing precondition in `findings.md` for the batch journal.
2. Do not open an Issue — this is an environment gap, not a product bug.
3. Add a note in the scenario's YAML `description` field to document the
   precondition requirement.

---

## Step 6 — Coverage check

Run after every full regression pass to surface uncovered features added since
the last batch.

```bash
reyn dogfood coverage dogfood/scenarios/*.yaml
```

Sample output:

```
Total features:   187
Covered:           42  (22.5%)
Uncovered:        145

Uncovered (sample):
  os-core/llm-validation/artifact-schema-validation
  control-ir-ops/sandboxed-exec
  stdlib-skills/skill-builder
  ...
```

**Decision rule**:

- Features added in the current release cycle that remain uncovered: open a
  follow-up task to author scenarios (not a bug Issue). Priority: HIGH if the
  feature is on the OS core path; MED otherwise.
- The 22.5% baseline established at FP-0036 land time grows incrementally as
  scenarios are authored. Track the covered % trend across releases — a
  declining trend means scenario authoring is falling behind feature growth.
- Unknown `covers:` tags (= tags that match no feature-map path) appear as
  warnings. Fix them by aligning the tag with `docs/feature-map.md`'s path
  scheme (lowercase kebab-case).

For JSON output:

```bash
reyn dogfood coverage dogfood/scenarios/*.yaml --json
```

---

## Step 7 — Report

Report writing follows `dogfood-reporting.md` exactly. This step delegates to
that document; the summary here is for sequencing only.

```bash
# Print the run's 4-band breakdown before writing the journal.
reyn dogfood report <candidate_run_id>
```

Journal steps:

1. Create `docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-<tag>>/`.
2. Copy templates from `docs/deep-dives/contributing/templates/`.
3. Fill `summary.md`, `findings.md`, `retrospective.md`.
4. Move `report.json` from the run directory to the journal directory.
5. Open a GitHub Discussion thread in the `Dogfood batches` category.
6. For each HIGH+ severity finding, open a `dogfood-finding` Issue.

The autonomous driver discipline applies (memory `feedback_dogfood_driver_role.md`):
complete the journal through the GitHub Discussion post without stopping to ask
for instructions. Pause only if a CRITICAL finding requires a decision that
blocks the release.

---

## Step 8 — Fix wave (when regressions found)

When true regressions are identified in Step 5 Category 1, dispatch a fix wave
following `dogfood-discipline.md` Section A5.

After the fixes land:

```bash
# Re-run candidate against the fixed SUT.
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5

# Compare the pre-fix candidate against the post-fix candidate.
reyn dogfood compare <pre-fix-run-id> <post-fix-run-id>
```

Confirm: `regressed_scenarios` in the new comparison is empty for the scenarios
that were fixed. If any remain, the fix did not fully resolve the regression —
iterate.

Re-record fixtures so future replay runs reflect the fix:

```bash
# The post-fix run already recorded fresh fixtures.
# Verify fixture directory is updated.
ls -lt dogfood/fixtures/chat_router_smoke/
```

If the fix changes an event type, schema field, or artifact shape, the old
fixture will be detected as stale on the next replay run and auto-invalidated.
Re-record manually if you need to use replay mode before the next live run.

---

## Quick reference

| Step | Command |
|---|---|
| Pre-flight | `git rev-parse HEAD` + `curl localhost:4000/v1/models` |
| Baseline | `reyn dogfood run <set.yaml> --n 5 && reyn dogfood baseline <id> --label <name>` |
| Candidate | `reyn dogfood run <set.yaml> --n 5` |
| Replay | `reyn dogfood run <set.yaml> --replay <fixture_dir>` |
| Compare | `reyn dogfood compare <baseline> <candidate> --threshold 0.05` |
| Report | `reyn dogfood report <run_id>` |
| Coverage | `reyn dogfood coverage dogfood/scenarios/*.yaml` |
| Post-fix verify | `reyn dogfood compare <pre-fix-run> <post-fix-run>` |

**Stability shot count guidance**:

| Context | `--n` |
|---|---|
| Fast smoke (low-risk PR) | `--n 1` |
| Standard regression | `--n 5` |
| Release gate | `--n 5` (min) |
| High-stakes / attractor claim | `--n 10` |

---

## Cross-references

- `dogfood-discipline.md` — methodology (= 9 principles, A1-A5 loop, Brier scoring)
- `dogfood-reporting.md` — reporting layer (= journal + Discussions + Issues)
- `concepts/observability/dogfood-scenarios.md` — YAML schema, 4-band outcome, coverage mechanics
- `reference/cli/dogfood.md` — CLI subcommand reference
- `proposals/0036-dogfood-scenario-framework.md` — design rationale and open points
- Memory `feedback_dogfood_driver_role` — autonomous driver discipline
- Memory `feedback_dogfood_parallel_reyn_agent_isolation` — per-cwd + per-agent isolation
- Memory `feedback_pre_conclusion_observation_checklist` — active trigger before writing findings
