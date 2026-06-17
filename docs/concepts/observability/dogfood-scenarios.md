---
type: concept
topic: dogfood-scenarios
audience: [human, agent]
---

# Dogfood Scenario Framework

A regression-grade scenario suite that asserts three observation surfaces —
reply text, emitted P6 events, and workspace artifacts — against
`docs/feature-map.md` coverage. Scenarios are authored as YAML and reused
across Reyn releases as a continuous regression suite, not as one-shot batch
prep.

## Why

Three mechanisms exist today and were not unified before FP-0036:

| | `reyn eval` | Dogfood scenario framework |
|---|---|---|
| Entry point | `reyn run <skill>` | `reyn chat` (router decides skill) |
| Verification | Per-phase rubric (LLM judge) | reply + events + artifacts |
| Scope | One skill at a time | Feature-map coverage |
| Outcome scale | Binary pass/fail | 4-band (verified / inconclusive / refuted / blocked) |
| Use case | Per-skill regression | System-wide e2e regression |

The framework is **orthogonal** to `reyn eval`. It reuses the `judge_output`
op backend and the baseline comparison pattern, but the CLI surface and YAML
schema are distinct. It is also orthogonal to one-shot batch preludes — those
are Markdown prose, not machine-readable, not reusable across batches.

LLM stochasticity, replay cost, feature drift, and coverage gaps are the four
driving constraints:

- **Stochasticity** — assertions use stability bands, not binary pass/fail
- **Cost** — full-suite re-runs use LLMReplay fixtures (zero LLM cost)
- **Drift** — `reyn dogfood compare <baseline> <candidate>` surfaces
  regressions vs noise
- **Coverage** — `reyn dogfood coverage` lists uncovered feature-map entries

## Schema

Each scenario set is a YAML file under `dogfood/scenarios/`. The top-level
`covers:` lists features covered by the whole set; each scenario has its own
`covers:` that feeds the coverage matrix.

### Single-turn scenario

```yaml
type: dogfood_scenario_set
name: chat_router_smoke
description: Chat router intent dispatch + stdlib catalog dispatch smoke
covers:
  - chat-router/intent-routing
  - stdlib-skills/direct-llm

scenarios:
  - id: simple_greeting
    covers: [chat-router/intent-routing, stdlib-skills/direct-llm]
    input: "こんにちは、何ができますか?"
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

### Multi-turn scenario

Multi-turn scenarios use `prompts: [...]` instead of `input:`. The `expected`
block applies to the final turn unless `per_turn_expected` is specified.

```yaml
  - id: multi_turn_plan
    covers: [os-core/phase-engine/act-decide-loop]
    prompts:
      - "コードを改善してください"
      - "変更を適用してください"
    expected:
      events:
        sequence:
          - skill_run_spawned
          - skill_run_completed
```

`input` and `prompts` are mutually exclusive; the loader raises
`ScenarioLoadError` if both are present.

## Verification surfaces

Three verifiers run independently and their outcomes compose as worst-case:
one `refuted` verdict refutes the whole scenario.

### Reply

`kind` controls matching:

| kind | assertion |
|---|---|
| `judge` | `rubric` is a list of natural-language criteria; `judge_output` op scores each |
| `substring` | `value` string must appear anywhere in the reply |
| `exact` | `value` string matches the reply (trimmed) |
| `regex` | `value` pattern matches via `re.search` |

### Events

`must_emit` asserts event presence with optional count comparator
(`>=1`, `==2`, `<5`, …) and payload subset match. `must_not_emit` asserts
absence. `sequence` asserts an ordered subsequence of event types across the
run.

### Artifacts

Each `ArtifactAssertion` tests workspace state: presence/absence by `skill`
and/or `type`, with an optional `fingerprint` (SHA256 of normalised content)
for pinned regression.

## 4-band outcomes

Each verifier returns one of four bands:

- `verified` — assertion clearly passed
- `inconclusive` — insufficient signal to decide
- `refuted` — assertion clearly failed
- `blocked` — infrastructure failure (timeout, missing fixture, …)

Event and artifact verifier results dominate; `judge` is a tiebreaker on
`inconclusive` outcomes. See
[Dogfood discipline](../../deep-dives/contributing/dogfood-discipline.md) for
band semantics and Brier scoring methodology.

`outcome_prediction` declares an expected 4-band probability distribution
(must sum to 1.0 ± 0.001). Brier score measures calibration quality across
runs.

## Coverage

Each scenario's `covers:` tags map to feature paths in `docs/feature-map.md`.
The path scheme is lowercase kebab-case:

```
### OS Core          -> os-core
#### Phase Engine    -> os-core/phase-engine
| Act/Decide loop |  -> os-core/phase-engine/act-decide-loop
```

`reyn dogfood coverage` (or `--json` for machine consumption) reads all
scenario sets and reports:

```
Total features:   187
Covered:           42  (22%)
Uncovered:        145

Uncovered (sample):
  os-core/llm-validation/artifact-schema-validation
  control-ir-ops/sandboxed-exec
  stdlib-skills/skill-builder
  ...
```

Unknown tags (= tags that match no feature path) are surfaced as warnings
without failing the run.

## Regression workflow

```bash
# Record a baseline
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5 --baseline smoke-v1

# After a Reyn change, run candidate and compare
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5
reyn dogfood compare smoke-v1 <run_id>
```

`compare` reports regressions, newly-passing scenarios, and Brier drift.
Exit code 1 if any scenario regresses beyond `--threshold` (default 0.1).

## Replay mode

First-run fixtures are recorded to `dogfood/fixtures/<scenario_id>/` by the
LLMReplay integration. Subsequent runs with `--replay <fixture_dir>` use
recorded LLM responses — zero LLM cost, fully deterministic:

```bash
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --replay dogfood/fixtures/
```

Replay-mode runs are tagged in `run_id` so reports distinguish them from live
runs. Fixtures are re-recorded on every release tag; a schema mismatch forces
re-recording automatically.

Replay is built on `src/reyn/dev/testing/replay.py` (`LLMReplay`); see
[Replay tests guide](../../guide/for-reyn-developers/write-replay-tests.md) for
fixture recording mechanics.

## Cross-references

- [Reference: `reyn dogfood` CLI](../../reference/cli/dogfood.md) — subcommand
  reference (run / coverage / report / compare / baseline)
- [Dogfood discipline](../../deep-dives/contributing/dogfood-discipline.md) —
  4-band outcome scale, Brier scoring, 9-principle framework
- [Concepts: Evaluation](../observability/evaluation.md) — `reyn eval` (per-skill rubric,
  orthogonal surface)
- [Concepts: Events](../runtime/events.md) — P6 event types used in `must_emit`
  assertions
- [Concepts: Operational Intelligence](../data-retrieval/operational-intelligence.md) — indexing
  and querying the same P6 event log
- [Feature Map](../../feature-map.md) — coverage tag source-of-truth
