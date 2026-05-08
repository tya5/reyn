---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md, reyn.yaml]
---

# Add a Python preprocessor step

**Goal:** Run a Python function before the LLM call to enrich the input artifact with a deterministically computed field (statistics, normalization, structured parses).

## When to use

- The computation is deterministic and you want it run identically every time.
- It's expensive or error-prone for the LLM (numerical stats, regex parsing, JSON shape transforms).
- You'd rather pay code-review cost once than prompt-engineering cost forever.

## Two modes

| Mode | Sandboxing | Use for |
|------|------------|---------|
| `pure` | AST-validated, restricted builtins, allowlisted imports, subprocess | Standard math/stats/regex work |
| `trusted` | None — full Python | File I/O, custom packages, anything `pure` blocks |

Default to `pure`. Reach for `trusted` only when `pure` blocks something you actually need.

## Step 1 — write the function

`<skill_dir>/stats.py`:

```python
def compute(artifact):
    text = artifact["data"].get("text", "")
    return {"word_count": len(text.split())}
```

The function takes the input artifact and returns a JSON-serializable dict.

## Step 2 — declare it in the phase

`phases/draft.md`:

```yaml
---
type: phase
name: draft
input: user_message
preprocessor:
  - python:
      module: stats
      function: compute
      mode: pure
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: stats
---

Use `stats.word_count` to decide whether to summarize or expand the
text.
```

`output_schema` is required — the LLM needs to know the shape, and reyn won't run user code at compile time to infer it.

## Step 3 — declare permissions

In the phase frontmatter:

```yaml
permissions:
  python:
    - module: stats
      function: compute
      mode: pure
      timeout: 30
```

The `module`/`function` must match the preprocessor step.

## Step 4 — approve at startup

`pure` mode steps still need approval the first time:

```yaml
# reyn.yaml — pre-approve project-wide
permissions:
  python:
    pure: allow
```

For `trusted`:

```yaml
permissions:
  python:
    trusted: allow
```

…and run with `--allow-untrusted-python`.

## What `pure` mode disallows

- `open`, `eval`, `exec`, `__import__`, `compile`, `globals`, `locals`
- `subprocess` and other risky modules
- Imports outside the curated allowlist (`math`, `statistics`, `json`, `re`, `random`, `time`, `datetime`, …)

Extend the allowlist via `reyn.yaml`'s `permissions.python.allowed_modules`.

## See also

- [Reference: preprocessor](../../reference/dsl/preprocessor.md) — `python` step
- [Reference: permissions](../../reference/config/permissions.md) — `python` declarations
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `permissions.python`
