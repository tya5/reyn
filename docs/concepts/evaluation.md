---
type: concept
topic: evaluation
audience: [human, agent]
---

# Evaluation

reyn ships a **structured evaluation infrastructure** built on top of the P6 event log. Rather than bolting on a separate observability layer, reyn treats every skill execution as a potential evaluation artifact: the same append-only event stream that powers live debug output and crash recovery is the substrate for golden-dataset testing, CI gating, and external trace export.

**The key insight:** If every state change is already an event (P6), evaluation is a query over that log — not a new recording system.

## Why evaluation infrastructure matters

### Goodhart's Law and reward hacking

UC Berkeley (2026-04) demonstrated reward hacking across eight major benchmarks: once a benchmark becomes a target, optimizing for the metric drifts away from the underlying capability. The industry-standard response is **traceability** — not just "what score did this run get?" but "which skill version, which model, which dataset case, and which phase produced it?"

reyn's evaluation infrastructure is designed to answer that question without additional executions. The `skill_version_hash` (FP-0006) is recorded in every `run_skill_started` event, which means retrospective version-to-version comparison is P6 log aggregation — not re-running anything.

### CI/CD gates are now the industry baseline

Braintrust established CI/CD gates as a de facto standard: a PR that regresses a skill's pass rate blocks merge. reyn's `reyn eval run` command produces exit code 1 on threshold failure, making it a drop-in CI step.

### Data sovereignty requirements

Japanese enterprises frequently cannot send trace data to US-hosted SaaS. reyn's export adapter supports Langfuse self-hosted, OTLP (local Jaeger, Grafana), IETF Agent Audit Trail (file output), and a local file backend — no mandatory external dependency.

## Three-layer architecture

```
┌──────────────────────────────────────────────────┐
│  P6 event log  (append-only JSONL per run)       │  ← foundation
│  .reyn/events/<run_id>.jsonl                      │
└──────────────────────────────────────────────────┘
             ↓ Component A: export adapter
┌──────────────────────────────────────────────────┐
│  Export adapter                                  │  ← forwarding layer
│  Langfuse / OTLP / IETF Audit Trail / file       │
└──────────────────────────────────────────────────┘
             ↓ Component B: reyn eval
┌──────────────────────────────────────────────────┐
│  reyn eval run / report / compare                │  ← operator surface
│  Golden dataset runner + CI threshold gate       │
└──────────────────────────────────────────────────┘
```

### Layer 1: P6 event log (foundation)

Every run already produces a JSONL event log at `.reyn/events/<run_id>.jsonl`. This is the P6 guarantee (see [concepts/events.md](events.md)): every state change emits an event; the log is append-only and replayable. Evaluation infrastructure reads from this log — it does not add a new recording path.

The IETF Agent Audit Trail draft fields map naturally onto P6 event types:

| IETF field | P6 mapping |
|------------|-----------|
| `identity` | `chain_id` / `skill_name` |
| `timing` | `timestamp` (present on all events) |
| `routing` | `run_skill_started.state_dir` |
| `parameters` | `tool_executed.op` + `tool_executed.args` |

### Layer 2: export adapter (Component A)

An async adapter that forwards P6 events to external evaluation platforms after skill execution completes. Export failures emit a warning only — the P6 core write is independent and unaffected.

P7 compliance: the adapter reads only `type / timestamp / data` and has no knowledge of skill-specific field names. The generic event schema is forwarded as-is; skill-domain knowledge lives in the external tool's rubrics, not in the adapter code.

Supported backends: **Langfuse** (self-hosted or cloud), **OTLP** (OpenTelemetry — local Jaeger, Grafana, Honeycomb), **IETF Agent Audit Trail** (file output, draft spec), **file** (local `.reyn/traces/`, default).

### Layer 3: reyn eval (Component B)

The operator-facing surface. `reyn eval run` executes a skill against a golden JSONL dataset, compares `final_output` against `expected`, and exits with a non-zero code when the pass rate is below `--threshold`. `reyn eval report` summarises past results. `reyn eval compare` compares two skill versions using P6 log data — no additional executions required.

## Four-component map

| Component | Description | Dependency |
|-----------|-------------|-----------|
| **A** — Export adapter | P6 → Langfuse / OTLP / IETF / file | none |
| **B** — `reyn eval` command | Golden dataset runner + CI gate + report | Component A (optional) |
| **C** — Regression compare | Version-to-version diff from P6 logs | FP-0006 `skill_version_hash` |
| **D** — `judge_output` op | LLM scorer callable from any phase | none |

Components A, B, and D are independent of FP-0006 and can be used without Component C.

## Positioning

**P7 compliance.** The OS has no knowledge of skill-specific rubric content. The `judge_output` op (Component D) receives a `target` path and a `rubric` string supplied by the calling skill — the OS-side implementation knows only the score and whether the threshold passed. Skill-domain evaluation criteria never appear in OS code.

**OSS self-host support.** Langfuse and Grafana/Tempo are both OSS and self-hostable. The local file backend requires no external service. reyn does not mandate any SaaS dependency for evaluation.

**IETF Agent Audit Trail alignment.** The IETF draft (draft-sharif-agent-audit-trail) is under active development. reyn's export adapter produces output sympathetic to the draft's field requirements using the P6 event mapping above. The spec is noted as draft status in the exporter configuration.

## Competitive comparison

| Feature | Braintrust | Langfuse | Reyn |
|---------|-----------|---------|------|
| CI/CD eval gate | ✓ | ✓ | ✓ (`reyn eval run --threshold`) |
| Version regression compare | ✓ | partial | ✓ (P6 log aggregation, no re-runs) |
| External export | Braintrust SaaS only | Langfuse only | Langfuse / OTLP / IETF / file |
| Self-host support | ✗ | ✓ | ✓ (all backends) |
| IETF Agent Audit Trail | — | — | ✓ (draft compliance, Component A) |
| LLM scorer in skill phases | — | — | ✓ (`judge_output` op, Component D) |
| P7 OS/skill separation | N/A | N/A | ✓ (rubric is always skill-supplied) |

## Phase 1 scope

**Included (Components A, B, D):**

- File export backend (default, no configuration required)
- Langfuse, OTLP, and IETF export backends (configured in `reyn.yaml`)
- `reyn eval run` — golden dataset runner with CI threshold gate
- `reyn eval report` — past results summary
- `judge_output` op — LLM scorer callable from any phase
- Workspace isolation — eval runs do not contaminate the production workspace

**Deferred (Component C — requires FP-0006):**

- `reyn eval compare` — version-to-version regression comparison using `skill_version_hash`

## See also

- [Guide: Setup evaluation](../guide/evaluation.md) — quickstart, export setup, CI integration
- [Reference: `reyn eval`](../reference/cli/eval.md) — CLI flag reference
- [Concepts: events](events.md) — P6 event log foundation
- [Concepts: workspace](workspace.md) — workspace isolation for eval runs
- [Concepts: permission model](permission-model.md) — non-interactive pre-approval for eval
- [Reference: control-ir](../reference/runtime/control-ir.md) — `judge_output` op schema
- [FP-0007](../deep-dives/proposals/0007-evaluation-infrastructure.md) — design rationale and implementation spec (internal)
