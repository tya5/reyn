---
type: how-to
topic: evaluation
audience: [human]
---

# Setup evaluation

**Goal:** Run `reyn eval run` against a golden dataset, gate CI on pass rate, and optionally export traces to Langfuse or an OTLP backend.

## Prerequisites

- reyn installed (`pip install reyn`)
- A skill to evaluate — for example `my_skill`
- The skill has been run at least once interactively so permission approvals are recorded

---

## Quickstart (5 steps)

### Step 1 — Add an exporter to `reyn.yaml`

The file exporter is active by default (no config needed). For a local trace archive, add:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/
```

Traces are written asynchronously after each skill run. The exporter does not affect skill execution latency.

### Step 2 — Create a golden dataset

Create `eval/golden.jsonl` — one JSON object per line:

```jsonl
{"input": {"query": "Summarise the key points of async programming"}, "expected": {"summary": "Async programming allows non-blocking I/O..."}, "tags": ["smoke"]}
{"input": {"query": "What is a context manager?"}, "expected": {"summary": "A context manager manages resource lifecycle..."}, "tags": ["smoke"]}
{"input": {"query": ""}, "expected": null, "tags": ["edge-case", "empty-input"]}
```

Fields:

| Field | Required | Description |
|-------|----------|-------------|
| `input` | yes | Passed directly to the skill as the run input |
| `expected` | no | Used for `mode: exact` comparison; ignored for `mode: judge` |
| `tags` | no | Filter runs with `--tags smoke` |

### Step 3 — Run the eval

```bash
reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
```

Output:

```
=== Eval: my_skill [3 case(s)] ===
    model=standard

━━━ case: smoke/0 ━━━
  input: Summarise the key points of async programming
  ✓ score=0.91  passed

━━━ case: smoke/1 ━━━
  input: What is a context manager?
  ✓ score=0.87  passed

━━━ case: edge-case/empty-input ━━━
  input: (empty)
  ✗ score=0.31  failed

═══════════════════════════════════════════════════════
 ✗ 2/3 cases passed (66.7%)  threshold=0.8
 Results → .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
═══════════════════════════════════════════════════════
```

Exit codes: `0` = all cases passed, `1` = spec or dataset error, `2` = pass rate below threshold.

### Step 4 — View the report

```bash
reyn eval report my_skill
```

Output:

```
my_skill — 3 runs on record

  2026-05-14  dataset=eval/golden.jsonl  2/3 passed (66.7%)  model=standard
  2026-05-13  dataset=eval/golden.jsonl  3/3 passed (100%)   model=standard
```

For the full structured JSON:

```bash
cat .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
```

### Step 5 — Add a CI step

```yaml
# .github/workflows/eval.yml
name: Skill eval

on: [push, pull_request]

jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install reyn
      - run: reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

The job fails when pass rate drops below 0.8 — blocking the merge.

---

## Export setup

### Langfuse self-hosted

Langfuse is OSS and self-hostable. Recommended for environments with data sovereignty requirements.

```yaml
# reyn.yaml
eval:
  exporters:
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://langfuse.your-domain.example.com
```

Set the keys via `reyn secret`:

```bash
reyn secret set LANGFUSE_PUBLIC_KEY
reyn secret set LANGFUSE_SECRET_KEY
```

Traces appear in Langfuse under the skill name as the trace name. Each phase visit maps to a span.

### OTLP (Jaeger, Grafana Tempo)

For an OpenTelemetry-compatible backend:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: otlp
      endpoint: http://localhost:4317   # gRPC — local Jaeger
```

For Grafana Cloud OTLP:

```yaml
eval:
  exporters:
    - type: otlp
      endpoint: https://otlp-gateway-prod-us-central-0.grafana.net/otlp
      headers:
        Authorization: Basic ${GRAFANA_OTLP_TOKEN}
```

### IETF Agent Audit Trail

The IETF Agent Audit Trail draft (draft-sharif-agent-audit-trail) defines a structured log format covering identity, timing, routing, and parameters. reyn's export maps P6 events to the draft's required fields.

```yaml
# reyn.yaml
eval:
  exporters:
    - type: ietf_audit
      path: .reyn/audit/
      # Note: IETF draft spec — format may change before standardisation
```

Audit files are written per-run to `.reyn/audit/<run_id>.jsonl`.

### Multiple exporters

Exporters are additive. Export to both a local file and Langfuse:

```yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://langfuse.your-domain.example.com
```

---

## Using `judge_output` in a skill phase

`judge_output` is a Control IR op that lets a phase score its own output against a rubric, then decide whether to continue or transition based on the result. The rubric is always supplied by the skill; the OS evaluates it without knowing the domain.

Example: an article-writing skill that self-evaluates before finishing:

```yaml
# phases/evaluate.md
---
input_schema: draft_article
---

Review the article draft in the workspace. Score it against the rubric.
If the score is below threshold, revise. Otherwise, finish.
```

The LLM emits a `judge_output` Control IR op:

```json
{
  "op": "judge_output",
  "target": "artifact.data.body",
  "rubric": "Score 0.0-1.0: Does the article clearly state the main argument in the first paragraph? Is each section supported by at least one concrete example?",
  "threshold": 0.75,
  "on_fail": "transition"
}
```

`on_fail` values:

| Value | Behaviour |
|-------|-----------|
| `transition` | LLM selects the next phase (typically a revise phase) |
| `abort` | Skill execution aborts immediately |
| `continue` | Execution continues regardless of score; score is recorded in the workspace |

The score is recorded in the P6 event log as `tool_executed` (op=judge_output, score=0.72, passed=false).

---

## Workspace isolation

Each `reyn eval run` case executes in an isolated workspace copy. Production workspace state — indexed sources, approvals, existing artifacts — is not visible to eval cases. Results from one case do not bleed into the next.

This isolation is guaranteed even when eval runs in the same project directory as normal skill runs. The `.reyn/eval-results/` output directory is the only shared write path between the eval runner and the project workspace.

### Non-interactive permissions

`reyn eval run` does not show permission prompts. Pre-approve any permissions the skill needs before running eval:

**Option 1 — Run interactively once:**

```bash
reyn run my_skill '{"query": "test"}'
# Accept the permission prompts — choices persist to .reyn/approvals.yaml
```

**Option 2 — Pre-approve in `reyn.yaml`:**

```yaml
permissions:
  python.safe: allow
  file.write: allow
```

**Option 3 — Operator-local override (gitignored):**

```yaml
# reyn.local.yaml (gitignored — for local CI or dogfood automation)
permissions:
  python.safe: allow
  python.unsafe: allow
```

See [Concepts: permission model](../concepts/runtime/permission-model.md) for the full three-layer pre-approval model.

---

## Troubleshooting

**`reyn eval run` exits with code 1 and says "spec failed to load"**

This indicates a problem reading the dataset file or parsing the JSONL. Check that each line is valid JSON:

```bash
python -c "import json; [json.loads(l) for l in open('eval/golden.jsonl')]"
```

**Cases are reported as "not-finished" instead of "failed"**

The skill encountered a permission gate during eval (which does not prompt). Pre-approve the required permissions using one of the options above. The event log for the failed case will show the `permission_denied` event:

```bash
reyn events .reyn/events/<run_id>.jsonl --filter permission_denied
```

**Scores are lower than expected for `mode: judge`**

The `judge_output` rubric drives the score. Vague rubrics ("the output is good") produce unreliable scores. Rewrite rubrics as concrete, testable statements:

- Vague: "The summary is well-written"
- Concrete: "The summary is 2-4 sentences. The first sentence states the main conclusion."

---

## See also

- [Concepts: evaluation](../concepts/observability/evaluation.md) — architecture and positioning
- [Reference: `reyn eval`](../reference/cli/eval.md) — full CLI flag reference
- [Concepts: events](../concepts/runtime/events.md) — P6 event log
- [Concepts: workspace](../concepts/runtime/workspace.md) — workspace isolation model
- [Concepts: permission model](../concepts/runtime/permission-model.md) — non-interactive pre-approval
- [Getting started: Writing an eval](getting-started/05-writing-an-eval.md) — rubric-based eval with `eval_builder`
