---
type: tutorial
topic: os-development
audience: [human]
---

# Run SWE-bench

This how-to covers running Reyn against [SWE-bench](https://www.swebench.com/)
(the standard coding-agent benchmark) to measure how the general agent solves
real GitHub issues. There are two surfaces, and they are independent:

- **Solve a single instance** — `scripts/swe_bench_runner.py` runs the general
  agent on one SWE-bench instance and emits a prediction (a git patch). It does
  **not** score.
- **Run a batch** — `reyn eval benchmark` runs a skill across a JSONL task file
  with concurrent dispatch and **built-in faithful scoring**.

Scoring in both cases is delegated to the official `swebench` harness, which is
an **optional dependency** — see [Faithful scoring](#faithful-scoring-and-the-honest-skip).

## Prerequisites

| Requirement | Why |
|---|---|
| Reyn installed (`reyn --version`) | the solver |
| Docker running | faithful runs use the official pre-built SWE-bench instance images |
| `pip install swebench` | **optional** — required only for authoritative Tier-1 scoring; see below |

A SWE-bench instance is a JSON object in the standard dataset format:

```json
{
  "instance_id": "django__django-1234",
  "repo": "django/django",
  "base_commit": "abc123...",
  "problem_statement": "...",
  "hints_text": "...",
  "test_patch": "..."
}
```

`hints_text` and `test_patch` are optional. The agent solves from the issue and
the repository only — the held-out `test_patch` is **not** put in the prompt;
the harness applies it at scoring time.

## Solve a single instance

`scripts/swe_bench_runner.py` solves one instance with the general agent and
prints one prediction JSON object on stdout:

```bash
# Faithful run: solve inside the official pre-built instance image
python scripts/swe_bench_runner.py \
  --input instance.json \
  --env-backend docker \
  --image swebench/sweb.eval.x86_64.django__django-1234:latest \
  --repo-dir /testbed

# Or read the instance JSON from stdin
cat instance.json | python scripts/swe_bench_runner.py --stdin --env-backend docker --image <IMAGE>
```

| Flag | Default | Notes |
|---|---|---|
| `--input PATH` / `--stdin` | — | **one is required** — the instance JSON source |
| `--env-backend host\|docker` | `host` | `docker` = faithful per-instance container run (recommended) |
| `--image IMAGE` | — | **required with `--env-backend docker`** — the pre-built SWE-bench instance image |
| `--repo-dir PATH` | `/testbed` | in-container repo working tree |
| `--model-name NAME` | `reyn` | value for the harness `model_name_or_path` field |
| `--timeout SECONDS` | `600` | max wall-clock for the solve |

Under `--env-backend docker` the runner owns the container lifecycle: it starts
the instance image, provisions a Python 3.11 venv with Reyn inside the
container, then drives the general agent (via `reyn run-once`) to explore →
edit → verify against the repo. Web tools are disabled so the agent cannot look
up the upstream fix. The repo's `git diff HEAD` becomes the prediction.

Output is a single JSON object on stdout:

```json
{"instance_id": "django__django-1234", "model_name_or_path": "reyn", "model_patch": "<git diff>"}
```

On failure (non-zero solve, timeout, or unparseable output) it emits
`{"instance_id": ..., "model_name_or_path": ..., "error": "..."}` and **still
exits 0**, so a batch loop over many instances is never aborted by one bad
instance. All progress and diagnostics go to stderr.

To score the prediction, feed it to the official `swebench` harness
(`python -m swebench.harness.run_evaluation`), or use the batch driver below,
which scores inline.

## Run a batch

`reyn eval benchmark` runs a skill across a JSONL task file (one task per line)
with concurrent dispatch, and scores each result inline:

> **There is no bundled `swe_bench` skill.** The batch driver runs whatever
> skill you name, and the previously bundled `swe_bench` skill was retired — so
> `reyn eval benchmark swe_bench …` will dead-end with "skill not found". The
> batch path requires a **caller-supplied** skill. The agent-routed (no-skill)
> SWE-bench solve is the [single-instance runner](#solve-a-single-instance)
> above; to cover a full dataset that way, loop the runner over instances and
> score the predictions with the `swebench` harness yourself. There is no single
> bundled command that runs the agent-routed solve over all of SWE-bench
> Verified **and** scores it.

```bash
reyn eval benchmark <SKILL> \
  --tasks swe_bench_verified.jsonl \
  --output results/ \
  --clone-task-repo \
  --concurrency 4 \
  [--limit 50] \
  [--resume]
```

| Flag | Default | Notes |
|---|---|---|
| `<SKILL>` | — | the skill to run on each task (resolved via reyn/project → local → stdlib) |
| `--tasks PATH` | — | **required** — JSONL; each line is one task input |
| `--output DIR` | — | **required** — results land under `<DIR>/run_<timestamp>/` |
| `--clone-task-repo` | off | **needed for SWE-bench** — clones `<repo>` and checks out `<base_commit>` into each task's workspace |
| `--concurrency N` | `4` | max concurrent runs |
| `--limit N` | all | stop after the first N tasks (after `--resume` filtering) |
| `--resume` | off | skip instances already completed in the latest prior run under `--output` |
| `--model MODEL` | from `reyn.yaml` | model override |

The batch auto-detects the verification environment once at start: with Docker
available it runs **Tier-1 faithful scoring** (the official SWE-bench image
applies your patch + the held-out test patch and reports the authoritative
verdict); otherwise every result is honest-skipped (see below).

Results land under `results/run_<timestamp>/`:

```
results/run_<timestamp>/
  summary.json          # faithful accounting + per-instance verdicts
  patches/<id>.diff     # extracted model patch (when the output carries one)
  logs/<id>.jsonl       # per-instance event log
```

The final stdout line and `summary.json` always show the **faithful
accounting**: how many results were faithfully verified, how many passed, and
how many were skipped. The authoritative harness verdict — not the agent's own
self-check — drives the pass count; a result is never counted as pass or fail
unless it was faithfully verified.

### Non-interactive permissions

`reyn eval benchmark` never prompts. Every permission the skill needs must be
pre-approved before the run, either by running the skill once interactively and
accepting the prompts, or by granting in `reyn.yaml`:

```yaml
permissions:
  python.safe: allow
  python.unsafe: allow   # also requires --allow-unsafe-python at runtime
```

Without prior approval a task fails and is reported as not-finished. See the
[`reyn eval` reference](../../reference/cli/eval.md#non-interactive-constraint)
and [Manage permissions](../for-users/manage-permissions.md).

## Faithful scoring and the honest-skip

Authoritative scoring is delegated to the official `swebench` harness, which is
an **optional dependency** Reyn does not install by default. The behaviour when
it is absent is the key gotcha to understand:

- **With `pip install swebench` + Docker**: each patch is scored by the official
  harness (applies the model patch + the held-out test patch in the pre-built
  image, runs the tests, reports `resolved`).
- **Without `swebench` installed**: scoring **honest-skips**. The result is
  marked `verify_skipped` with a clear reason — Reyn **never emits a fake
  PASS/FAIL**. In the batch, skipped results are excluded from the pass rate
  (which becomes `null` if nothing was faithfully verified), and the prominent
  accounting line reports the skip count.

So a batch can complete "successfully" with a `null` pass rate purely because
`swebench` was not installed — the patches were produced, but nothing was
authoritatively scored. Always check the faithful-verified / skipped counts in
the summary, not just that the run finished.

## See also

- [`reyn eval` reference](../../reference/cli/eval.md) — full `benchmark` flag reference
- [Configure the sandbox](../for-users/configure-sandbox.md) — how container/host execution is bounded
- [Manage permissions](../for-users/manage-permissions.md) — pre-approval mechanics
- [SWE-bench integration proposal](../../deep-dives/proposals/0008-swe-bench-integration.md) — the original design record
