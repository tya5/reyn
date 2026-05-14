# FP-0007: Agent Evaluation Infrastructure — P6 Trace Export + Skill Regression Evaluation

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Reyn already has a P6 event log, and the structure is in place to use it as an evaluation
infrastructure. This FP adds the following four components:
(A) An export adapter for P6 events to external evaluation tools (Langfuse / OTLP / IETF Agent Audit Trail),
(B) A `reyn eval` command for running golden datasets against skills and CI gating,
(C) Version-to-version regression comparison using FP-0006's `skill_version_hash`,
(D) A `judge_output` op (LLM scorer) callable from any phase.

---

## Motivation

### Industry Evaluation Infrastructure Trends (2026-05 survey)

In academic circles, SWE-bench Verified (coding) and METR Time Horizon (safety) are the most
cited benchmarks, but both face a structural criticism of "not reflecting real production behavior."
UC Berkeley (2026-04) demonstrated that reward hacking is possible across 8 major benchmarks,
and the work is widely cited as a concrete instance of Goodhart's Law ("when a measure becomes
a target, it ceases to be a good measure").

Enterprise landscape:
- **Braintrust**: CI/CD gates (blocking merges on score regression per PR) are the de facto standard
- **Langfuse**: OSS, self-hostable → a strong choice for Japanese enterprises (data sovereignty requirements)
- **IETF Agent Audit Trail**: A structured log standard covering `identity / timing / routing / parameters` is under development

### P6 Event Log as an Evaluation Infrastructure Foundation

One fundamental answer to the Goodhart problem is **traceability of which version produced a given score**.
Combining FP-0006's `skill_version_hash` with P6's append-only log enables automatic comparison of
"50 runs with skill v1 vs. 50 runs with skill v2" — with zero additional executions.

The required fields in the IETF Agent Audit Trail draft (draft-sharif-agent-audit-trail) map
naturally onto Reyn's P6 event types:

| IETF field | P6 event mapping |
|---|---|
| identity | chain_id / skill_name |
| timing | timestamp (common to all events) |
| routing | run_skill_started's state_dir |
| parameters | tool_executed's op + args |

---

## Proposed implementation

### Component A — P6 Event Export Adapter (MEDIUM)

An adapter that forwards P6 events to external evaluation tools.
To comply with P7, the adapter outputs a generic skill-agnostic event schema
(the adapter only reads `type / timestamp / data` and has no knowledge of skill-specific field names).

```python
# src/reyn/eval/export.py

class TraceExporter(Protocol):
    async def export(self, events: list[Event]) -> None: ...

class LangfuseExporter(TraceExporter): ...   # Self-hostable, for Japanese enterprises
class OTLPExporter(TraceExporter): ...       # OpenTelemetry standard
class IETFAuditExporter(TraceExporter): ...  # Compliant with IETF Agent Audit Trail draft
class FileExporter(TraceExporter): ...       # Local output to .reyn/traces/ (default)
```

Configuration:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://your-langfuse.example.com   # Self-hosted URL
    - type: otlp
      endpoint: http://localhost:4317
    - type: file                                 # Default (active even without config)
      path: .reyn/traces/
```

Export timing: sent asynchronously after skill execution completes (no impact on the execution path).
On failure, a warning is logged only (P6 core writes are independent).

### Component B — `reyn eval` Command (MEDIUM)

A CI gate mechanism that runs a skill against a golden dataset and records pass/fail and scores.

```
reyn eval run <skill_name> --dataset eval/golden.jsonl [--threshold 0.8]
reyn eval compare <skill_name> --from v1 --to v2        # Version-to-version regression comparison
reyn eval report <skill_name>                            # Summary of past eval results
```

**Golden dataset format** (JSONL):

```jsonl
{"input": {"query": "..."}, "expected": {"summary": "..."}, "tags": ["smoke"]}
{"input": {"query": "..."}, "expected": {"summary": "..."}, "tags": ["regression"]}
```

**`reyn eval run` behavior**:

1. Run the skill for each test case (workspace is isolated)
2. Compare `final_output` against `expected`
   - `mode: exact` — exact JSON match
   - `mode: judge` — score computed by the `judge_output` op (Component D)
3. Save results to `.reyn/eval-results/<skill>/<timestamp>.jsonl`
4. Record `skill_version_hash` in results (connection to FP-0006)
5. If pass rate is below `--threshold`, **exit code 1** → usable as a CI gate

**CI usage example**:

```yaml
# .github/workflows/eval.yml
- run: reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
```

### Component C — Skill Version Regression Comparison (SMALL)

Using FP-0006's `skill_version_hash`, compare a skill before and after changes against
the same dataset.
**No additional executions required** — achieved by aggregating P6 logs only.

```
reyn eval compare my_skill --from v1 --to v2

  v1 (sha:abc123):  72% pass  (36/50)  2026-05-01 ~ 2026-05-05
  v2 (sha:def456):  88% pass  (44/50)  2026-05-05 ~       ← current
  diff: +16pp  /  regression: none
```

`reyn eval compare` references:
- `.reyn/skill-versions/<name>/current` — current version
- P6 `run_skill_started` event's `skill_version_hash` — execution history per version
- `.reyn/eval-results/<skill>/` — results from explicit eval runs

### Component D — `judge_output` Op (SMALL)

An LLM scorer op callable from any phase in a skill.
Used both in the eval loop of a `run_and_eval` phase and as the comparison engine for `reyn eval run`.

**Control IR format**:

```json
{
  "op": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "Score on a scale from 0.0 to 1.0 according to the following criteria: ...",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

P7 compliance: the rubric content is supplied by the calling skill.
The OS-side `judge_output` implementation only receives the value at the `target` path and the
`rubric` string — it has no knowledge of skill-specific evaluation criteria.

`on_fail` values are OS-level vocabulary only:
- `"transition"` — LLM selects the next phase
- `"abort"` — abort skill execution
- `"continue"` — continue regardless of score (score is recorded in the workspace only)

Results are recorded in P6 as `tool_executed` (op=judge_output, score=0.72, passed=false).

**CLAUDE.md NEVER rule compliance**:
`control-ir.md` and `OP_KIND_MODEL_MAP` must be updated in the same PR (mandatory).

---

## Comparison with Hermes / Braintrust

| Feature | Braintrust | Hermes (unshipped) | Reyn (after this FP) |
|---|---|---|---|
| CI/CD eval gate | ✓ | — | ✓ (`reyn eval run`) |
| Version regression comparison | ✓ | — | ✓ (FP-0006 + Component C) |
| External export | Braintrust SaaS only | — | ✓ Langfuse / OTLP / IETF |
| Self-host support | ✗ | — | ✓ (Langfuse self-hosted) |
| IETF compliance | — | — | ✓ (Component A) |
| P7 compliance | N/A | N/A | ✓ (OS has no skill-specific knowledge) |

---

## Dependencies

- `src/reyn/events/events.py` — source of events for export (no changes)
- `src/reyn/op_runtime/registry.py` — add `judge_output` to `OP_KIND_MODEL_MAP`
- `docs/reference/runtime/control-ir.md` — add `judge_output` section (must be same PR as registry)
- FP-0006 (skill_version_hash) — prerequisite for Component C. A / B / D can be implemented independently

No prerequisite PRs: Components A / B / D can be implemented before FP-0006.
Only Component C requires FP-0006's `skill_version_hash`.

---

## Cost estimate

**Total: LARGE**

| Task | Cost | Notes |
|---|---|---|
| Component A: export adapter (Langfuse / OTLP / IETF / File) | MEDIUM | OTLP is well-spec'd; Langfuse has a public REST API |
| Component B: `reyn eval run` + golden dataset runner | MEDIUM | workspace isolation + pass/fail judgment + JSONL output |
| Component C: version regression comparison | SMALL | P6 log aggregation only, no new executions |
| Component D: `judge_output` op + registry + control-ir.md | SMALL | op implementation + doc update |
| Tests (Tier 1 / Tier 2) | SMALL | Component A export contract + Component D op contract |

Bottlenecks are **Component B's workspace isolation** (ensuring eval runs do not contaminate the
production workspace) and **Component A's IETF format accuracy** (the draft spec may still be evolving).

---

## Related

- `src/reyn/events/events.py` — P6 event foundation
- `src/reyn/op_runtime/registry.py` — OP_KIND_MODEL_MAP
- `docs/reference/runtime/control-ir.md` — op catalog (target for judge_output addition)
- FP-0006 (`0006-skill-self-improvement.md`) — skill_version_hash (prerequisite for Component C)
- `docs/deep-dives/research/landscape/hn-practitioner-voice-2026.md` — HN observability criticism
- [IETF Agent Audit Trail draft](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/)
- [Langfuse OSS](https://langfuse.com/) — self-hostable evaluation platform

---

## User documentation

The following user-facing docs were created as part of the FP-0007 documentation wave:

| Document | Path | Description |
|----------|------|-------------|
| Concept doc | `docs/concepts/evaluation.md` | Architecture, 3-layer model, competitive comparison |
| Concept doc (JA) | `docs/concepts/evaluation.ja.md` | Japanese translation |
| Operator guide | `docs/guide/evaluation.md` | Quickstart, export backends, CI integration, `judge_output` usage |
| Operator guide (JA) | `docs/guide/evaluation.ja.md` | Japanese translation |
| CLI reference | `docs/reference/cli/eval.md` | `reyn eval run` + `reyn eval report` flag reference (appended) |
| CLI reference (JA) | `docs/reference/cli/eval.ja.md` | Japanese translation (appended) |
