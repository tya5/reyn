# Run SWE-bench Verified

This guide walks through using Reyn to solve [SWE-bench Verified](https://www.swebench.com/) problems — from a single-instance smoke test to the full 500-problem batch.

> **TL;DR**: install reyn, point `scripts/swe_bench_runner.py` at an instance JSON, and you get a harness-compatible patch JSON on stdout. For batch runs, use `reyn eval benchmark swe_bench`.

## Prerequisites

- **Python 3.11+** with `reyn` installed and `reyn` on `PATH`.
- **A configured LLM** — set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or whichever provider your `reyn.yaml` points at.  The `swe_bench` skill uses the `shell` op and needs file read/write; no additional permissions setup is required because `shell: true` and `file.write: ["*"]` are declared in the skill's `skill.md`.
- **Git** available in `PATH` (the skill runs `git checkout`, `git diff`).
- **The target repo's test runner** (e.g. `pytest`, `tox`) installed in the environment where `reyn run` executes — or inside the Docker container if you're using the official harness.
- *(Optional)* **Docker** if you're connecting to the official SWE-bench evaluation harness.
- *(Optional)* **Hugging Face datasets** to download SWE-bench Verified locally:

```bash
pip install datasets
python -c "
from datasets import load_dataset
ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='test')
ds.to_json('swe_bench_verified.jsonl', orient='records', lines=True)
"
```

## Single-instance smoke test

1. Save one SWE-bench instance to a file:

```json
{
  "instance_id": "django__django-16820",
  "repo": "django/django",
  "base_commit": "0fbdb9784da915fce5dcc1fe82bac9b4785749e5",
  "problem_statement": "...",
  "hints_text": "...",
  "test_patch": "diff --git ..."
}
```

2. Run the wrapper:

```bash
python scripts/swe_bench_runner.py --input instance.json
```

Expected stdout (one JSON line):

```json
{"instance_id": "django__django-16820", "model_name_or_path": "reyn", "model_patch": "diff --git a/django/..."}
```

Progress messages go to stderr so you can redirect stdout cleanly:

```bash
python scripts/swe_bench_runner.py --input instance.json > patch_record.jsonl
```

If reyn fails (non-zero exit, timeout, or unparseable output) the wrapper still exits 0 with an error record:

```json
{"instance_id": "django__django-16820", "model_name_or_path": "reyn", "error": "reyn exited 1: ..."}
```

## Small subset (1–10 problems)

Use `reyn eval benchmark` to run several instances concurrently and collect structured results:

```bash
# Extract a 10-problem subset
head -10 swe_bench_verified.jsonl > subset.jsonl

reyn eval benchmark swe_bench \
  --tasks subset.jsonl \
  --output results/ \
  --limit 10 \
  --concurrency 4
```

Results land in `results/run_<timestamp>/`:

```
results/run_20260528_120000/
  summary.json          — pass rate, cost, timing
  patches/
    django__django-16820.diff
    ...
  logs/
    django__django-16820.jsonl   — per-instance P6 event log
```

The `summary.json` looks like:

```json
{
  "run_id": "run_20260528_120000",
  "skill": "swe_bench",
  "total": 10,
  "completed": 10,
  "passed": 7,
  "pass_rate": 0.70
}
```

Re-run interrupted batches with `--resume`:

```bash
reyn eval benchmark swe_bench \
  --tasks subset.jsonl \
  --output results/ \
  --resume
```

## Full 500-problem run

Same shape as above, without `--limit`:

```bash
reyn eval benchmark swe_bench \
  --tasks swe_bench_verified.jsonl \
  --output results/ \
  --concurrency 8
```

**Cost and time expectations** (approximate, using `claude-sonnet` / `gemini-flash` class models):

| Concurrency | Wall time | Estimated cost |
|---|---|---|
| 4 | ~8–12 hours | ~$150–200 |
| 8 | ~4–6 hours  | ~$150–200 |
| 16 | ~2–3 hours | ~$150–200 (same total; concurrency trades time for parallelism) |

Cost per instance is dominated by the explore + apply + verify loop (~$0.30–0.50 per instance). Problems that require multiple verify/apply cycles cost more.

## Connecting to the SWE-bench harness

The [official SWE-bench harness](https://github.com/princeton-nlp/SWE-bench) expects a callable that receives an instance and produces a `model_patches.jsonl` entry. Wire `swe_bench_runner.py` as the model callback:

1. Inside the harness Docker container (or host), make sure `reyn` is on `PATH`:

```bash
pip install reyn
reyn --version   # confirm
```

2. Point the harness at the wrapper. The harness typically expects a script that reads instances from a JSONL and writes patch records. A minimal adapter loop:

```bash
#!/usr/bin/env bash
# run_reyn_batch.sh — adapter for the SWE-bench harness
while IFS= read -r line; do
    echo "$line" | python scripts/swe_bench_runner.py --stdin --model-name reyn
done < "$1"
```

Or, if the harness calls your script once per instance:

```bash
# The harness passes the instance JSON as the first argument or via stdin.
python scripts/swe_bench_runner.py --stdin --model-name reyn
```

3. Collect `model_patches.jsonl` by appending wrapper output:

```bash
while IFS= read -r instance; do
    echo "$instance" \
        | python scripts/swe_bench_runner.py --stdin \
        >> model_patches.jsonl
done < swe_bench_verified.jsonl
```

Then hand `model_patches.jsonl` to the harness evaluator as usual. See the [SWE-bench README](https://github.com/princeton-nlp/SWE-bench) for the evaluation step.

## Troubleshooting

**`reyn: command not found`** — `reyn` is not on `PATH`. Either activate the virtualenv that has reyn installed, or use `--reyn-cmd 'python -m reyn'`:

```bash
python scripts/swe_bench_runner.py --input instance.json \
    --reyn-cmd 'python -m reyn run swe_bench'
```

Wait — the `--reyn-cmd` flag replaces the base command list (`reyn run swe_bench`), not just `reyn`. Provide the full prefix up to but not including the input argument.

**`missing required field(s): base_commit`** — the instance JSON is incomplete.  SWE-bench Verified instances all have `base_commit`; if you're building a custom instance, add it.

**`Error: cannot read input file`** — the `--input` path does not exist or is not readable. Check the path.

**`reyn exited N: ...`** — reyn itself failed. Common causes:
- No LLM credentials: set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / the relevant env var for your model provider.
- No models configured: run `reyn config` or add a `model:` entry to `reyn.yaml`.
- The repo clone failed: ensure the repository is cloned at the path the skill expects, or that `git` can access the remote.
- `shell: true` permission not granted: the `swe_bench` skill declares `shell: true` in its frontmatter; `reyn run` respects this automatically for stdlib skills, so you should not need `--allow-shell`.

**`could not find 'patch' field in reyn output`** — reyn ran but did not produce a `swe_bench_result` artifact with a `patch` field. This means the skill aborted or the `report` phase failed. Check the stderr output for the `reyn run` diagnostic lines, or run the instance manually:

```bash
reyn run swe_bench '{"instance_id": "...", ...}'
```

**Timeout** — increase `--timeout` (default 600 s). Complex problems can take 10–15 minutes on slower hardware:

```bash
python scripts/swe_bench_runner.py --input instance.json --timeout 1200
```

## Related

- [`src/reyn/stdlib/skills/swe_bench/skill.md`](../../../src/reyn/stdlib/skills/swe_bench/skill.md) — skill definition, phase graph, permissions
- [`reyn eval benchmark` reference](../../reference/cli/eval.md) — all flags for the batch runner
- [SWE-bench harness](https://github.com/princeton-nlp/SWE-bench) — official evaluation infrastructure
