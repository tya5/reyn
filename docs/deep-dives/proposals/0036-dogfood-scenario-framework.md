# FP-0036: Dogfood Scenario Framework — feature-mapped regression-grade scenario sets

**Status**: proposed
**Proposed**: 2026-05-16
**Author**: Session continuation (post FP-0001 / FP-0009 / FP-0016 closure)

---

## Summary

Define a regression-grade scenario set framework for large-scale e2e dogfood
that asserts three observation surfaces (`reply` natural language / `events`
log / `artifacts` workspace) against feature-map coverage. Scenarios are
authored as YAML, run by a new `reyn dogfood` CLI, and reused across
batches as a regression suite — not as one-shot batch prep.

---

## Motivation

### Current state — partial scenario infra, no expected surface

Three things exist today and they are not unified:

1. **`dogfood/scenarios/*.yaml`** — input-only prompts (`long_session_v1.yaml`
   has multi-turn sequences with `kind:` tags), no expected fields, no runner.
2. **`prelude.md` per batch** — Markdown prose describing expected behaviour
   for each scenario; not machine-readable, not reusable across batches.
3. **`reyn eval`** (FP-0007) — per-skill golden dataset + quality rubric
   evaluator with `reyn eval compare` for `skill_version_hash`-keyed
   regression diff. Skill-bound, not chat-router-level.

Combined gap: there is no way to declare a chat-router-level scenario whose
expected behaviour spans reply text + emitted events + produced artifacts,
and to track its pass/fail over time as the OS evolves.

### Regression-first design constraint

The user's stated intent for this framework: **the scenario set is used as a
regression suite, repeatedly, across Reyn releases**. This is qualitatively
different from one-shot batch dogfood:

- LLM stochasticity → assertions must accommodate N>1 stability bands, not
  binary pass/fail
- LLM cost → re-runs of the full suite must have a low-cost replay path
  (= LLMReplay fixture)
- Behaviour drift → comparison against a stored baseline must surface
  regression vs noise (= cf. `reyn eval compare`'s `--threshold` rule)
- Feature coverage → as the OS adds features, the suite must surface
  uncovered features and accept new scenarios incrementally

### Why this is not a fit for FP-0007 (`reyn eval`) extension

| Aspect | `reyn eval` today | Dogfood scenario framework |
|---|---|---|
| Entry point | `reyn run <skill>` | `reyn chat` (router decides skill) |
| Verification surface | Per-phase rubric (LLM judge) | reply + events + artifacts |
| Scope | One skill at a time | Feature-map coverage |
| Outcome scale | Binary pass/fail | 4-band (verified / inconclusive / refuted / blocked) |
| Use case | Per-skill regression | System-wide e2e regression |

Surfaces are orthogonal. Conflating them dilutes both. We reuse the LLM judge
backend (`judge_output` op) and the `skill_version_hash`-baseline pattern, but
the CLI surface and YAML schema stay distinct.

---

## Proposed implementation

### Component A — Scenario YAML schema + loader (MEDIUM)

`src/reyn/dogfood/scenarios.py` — new module under `src/reyn/dogfood/`.

```yaml
# dogfood/scenarios/chat_router_smoke.yaml
---
type: dogfood_scenario_set
name: chat_router_smoke
description: Chat router intent dispatch + stdlib catalog dispatch smoke
covers:
  - chat-router/intent-routing
  - stdlib-skill/direct_llm

scenarios:
  - id: simple_greeting
    covers: [chat-router/intent-routing, stdlib-skill/direct_llm]
    input: "こんにちは、 何ができますか?"
    expected:
      reply:
        kind: judge
        rubric:
          - explains capabilities at high level
          - mentions chat / skills / agents
      events:
        must_emit:
          - { type: skill_run_spawned, count: ">=1" }
          - { type: skill_run_completed, status: success }
        must_not_emit:
          - { type: permission_denied }
      artifacts:
        - { skill: direct_llm, present: true }
      outcome_prediction:
        verified: 0.7
        inconclusive: 0.2
        refuted: 0.05
        blocked: 0.05
```

Multi-turn scenarios reuse the existing `prompts: [...]` shape; expected
applies to the final turn unless `per_turn_expected` is specified.

### Component B — Runner CLI (MEDIUM)

```
reyn dogfood run <scenario_set.yaml>           # single run
reyn dogfood run <set> --n 5                   # N runs for stability band
reyn dogfood run <set> --baseline <run_id>     # record this run as baseline
reyn dogfood run <set> --replay <fixture_dir>  # deterministic, no LLM cost
reyn dogfood coverage [--feature-map docs/feature-map.md]
reyn dogfood report <run_id>                   # 4-band breakdown + Brier
reyn dogfood compare <baseline> <candidate>    # regression diff
```

Storage:

```
.reyn/dogfood/runs/<run_id>/
  scenarios/<scenario_id>/
    output.json         # reply text + verifier verdicts
    events.jsonl        # captured P6 event tail
    artifacts/          # workspace snapshot
  summary.json          # 4-band aggregate + Brier
.reyn/dogfood/baselines/<label>/  # symlink or copy of a run_id
```

### Component C — Verifier triad (SMALL)

`src/reyn/dogfood/verifiers/`:

- `reply.py` — kinds: `judge` (uses `judge_output` op), `substring`, `exact`,
  `regex`
- `events.py` — `must_emit` (type + count + payload pattern), `must_not_emit`,
  sequence (= ordered subsequence)
- `artifacts.py` — presence by `skill` / `type`; optional `fingerprint`
  (= SHA256 of normalised content)

Outcome composition: each verifier returns `verified / inconclusive / refuted
/ blocked`. The scenario's overall outcome is the worst-case (= one refuted
verifier refutes the scenario).

### Component D — Feature-map coverage matrix (SMALL)

`src/reyn/dogfood/coverage.py`:

- Parses `docs/feature-map.md`'s table rows into feature paths
  (= `os-core/phase-engine/act-decide-loop`)
- Walks scenarios across one or more YAML sets, collects `covers:` tags
- Produces a matrix (= covered feature count, uncovered feature list)
- `reyn dogfood coverage` prints it; `--json` for machine consumption

### Component E — Baseline + regression compare (SMALL)

`src/reyn/dogfood/compare.py`:

- Mirrors `reyn eval compare`'s pattern (= `--threshold`, exit-1 on regression)
- Compares a candidate run against a baseline run on the 4-band distribution
  + per-scenario outcome
- Reports: regressed scenarios, newly-passing scenarios, drift in Brier

### Component F — LLMReplay fixture integration (SMALL)

`src/reyn/dogfood/replay.py`:

- Reuses `src/reyn/testing/replay.py` LLMReplay class
- First run records fixtures to `dogfood/fixtures/<scenario_id>/`
- Subsequent `reyn dogfood run --replay` invocations use the fixture for
  deterministic, zero-LLM-cost re-runs
- Replay-mode runs are tagged in `run_id` so reports distinguish them

---

## Dependencies

- `docs/feature-map.md` (= coverage tag source-of-truth, exists)
- `src/reyn/testing/replay.py` LLMReplay (existing)
- `src/reyn/op_runtime/judge_output.py` (FP-0007 D, landed)
- `reyn eval compare` patterns (= baseline auto-selection rule, --threshold)
- Dogfood discipline 4-band outcome scale (`docs/deep-dives/contributing/
  dogfood-discipline.{md,ja.md}`)

No OS layer change. Implementable entirely outside the kernel (P7 compliant).

---

## Cost estimate

**Total: MEDIUM-LARGE** — ~1-2 day MVP, 1 additional day for full coverage matrix + replay.

| Component | Size | Notes |
|---|---|---|
| A: Scenario schema + loader | MEDIUM | YAML parsing + validation + scenario data model |
| B: Runner CLI | MEDIUM | run / coverage / report / compare / baseline subcommands |
| C: Verifier triad | SMALL | reply / events / artifacts, each ~50-100 lines |
| D: Coverage matrix | SMALL | Parser for feature-map.md + tag aggregation |
| E: Baseline compare | SMALL | Reuse compare patterns from `reyn eval compare` |
| F: LLMReplay integration | SMALL | Thin wrapper around existing `LLMReplay` |
| Tests | MEDIUM | Tier 1/2/3 across all components |
| Scenario set authoring | LARGE | Out of scope for the framework PR — separate follow-up |

The scenario set itself (= per-feature scenarios for `docs/feature-map.md`) is
LARGE in aggregate but is authored AFTER the framework lands and is dispatched
as a separate wave.

---

## Open design points (= resolve before implementation)

1. **Scenario set granularity**: one big file or per-category files?
   Recommendation: per-category files in `dogfood/scenarios/` mirroring the
   feature-map sections (= `chat_router.yaml`, `control_ir_ops.yaml`,
   `stdlib_skills.yaml`, `permissions.yaml`, ...). Coverage runs across all.

2. **Baseline storage location**: `.reyn/dogfood/baselines/` (= per-developer)
   or in repo (= shared)? MVP: per-developer; revisit if team wants shared
   baselines via CI.

3. **Replay fixture freshness**: how often do fixtures need re-recording?
   Recommendation: re-record on every release tag; flag fixture-vs-runtime
   schema mismatch and force re-record automatically.

4. **Outcome composition**: should the framework override LLM judge with the
   harder event/artifact verifiers? Recommendation: yes — event/artifact
   results dominate; judge is only a tiebreaker on `inconclusive` outcomes.

---

## Related

- `docs/feature-map.md` — coverage taxonomy source-of-truth
- `dogfood/scenarios/long_session_v1.yaml` — existing input-only YAML pattern
- `docs/deep-dives/contributing/dogfood-discipline.{md,ja.md}` — 4-band
  outcome + 9-principle framework + multi-shot pattern
- `src/reyn/testing/replay.py` — LLMReplay (reusable)
- `docs/reference/cli/eval.md` — `reyn eval compare` (regression diff prior art)
- `docs/deep-dives/proposals/0007-evaluation-infrastructure.md` — eval/spec
  for per-skill rubric (= orthogonal surface)
