---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn eval]
---

# `reyn eval`

Run an eval spec against a target skill non-interactively. Each case is judged phase-by-phase against rubric criteria; per-case results plus an overall summary are written to `.reyn/eval_reports/`.

## Synopsis

```
reyn eval [OPTIONS] FILE
```

## Positional arguments

| Name | Description |
|------|-------------|
| `FILE` | Path to the eval spec markdown (e.g. `reyn/local/my_skill/eval.md`). The spec references the target skill via its `skill_dsl_path` frontmatter field. |

## Options

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model class (`light`/`standard`/`strong`) or LiteLLM model string. **Precedence:** CLI > spec > `reyn.yaml`. |
| `--dsl-root DIR` | DSL root override for the target skill. Inferred from the skill path by default. |
| `--output-language LANG` | Output language code passed to both eval skill and target skill. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per case. `0` = unlimited. Default from `reyn.yaml` or `25`. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | All cases passed |
| `1` | Spec failed to load (e.g. malformed eval.md) |
| `2` | One or more cases failed their criteria |

## Output

A summary line per case is printed to stdout:

```
━━━ case: short_summary ━━━
  input: reyn is a workflow OS for LLMs.
  ✓ score=0.95  (4/4 required)
```

The full structured report is written to `.reyn/eval_reports/<target_skill>/<timestamp>.json` and the path is printed on the final line.

## Non-interactive constraint

`reyn eval` does not prompt. Every permission the target skill needs must be pre-approved:

- run the target once interactively (`reyn run <target> "<sample>"`) and accept the prompts — choices persist to `.reyn/approvals.yaml`, OR
- set project-wide grants in `reyn.yaml`:

```yaml
permissions:
  python.pure: allow
  python.trusted: allow   # also requires --allow-untrusted-python at runtime
```

Without prior approval the target run fails and the case is reported as not-finished. The framing reads as a target-skill bug, but the cause is missing approvals.

## Examples

Run the eval bundled with a project skill:

```bash
reyn eval reyn/project/article_writer/eval.md
```

Override the model just for this run:

```bash
reyn eval reyn/local/my_skill/eval.md --model strong
```

Iterate during development (use a cheap model, single case):

```bash
reyn eval reyn/local/my_skill/eval.md --model light
```

## See also

- [run.md](run.md) — `reyn run` (the underlying execution path)
- [Reference: stdlib/eval](../stdlib/eval.md) — what the eval skill produces
- [Reference: stdlib/eval_builder](../stdlib/eval_builder.md) — generate spec files
- [Reference: permissions](../config/permissions.md) — pre-approval mechanics
