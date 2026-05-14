---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn lint]
---

# `reyn lint`

Run deterministic structural checks on a skill directory: graph, frontmatter, artifact references, and Python preprocessor steps (when `mode: safe`, the AST is also validated). Detects most authoring mistakes before runtime.

## Synopsis

```
reyn lint SKILL
```

## Positional arguments

| Name | Description |
|------|-------------|
| `SKILL` | Skill name. Same resolution as [`reyn run`](run.md): `reyn/project/` → `reyn/local/` → stdlib. |

## What is checked

- **Graph**: every key references a phase file in `phases/`; every value is a known phase, sub-skill (`@name`), or `end`.
- **Reachability**: every phase reachable from `entry`; phases with `can_finish: true` have a path to `end`.
- **Frontmatter**: required keys (`type`, `name`, `entry`, `final_output`).
- **Artifact references**: every `input` and `final_output_schema` resolves to an artifact file.
- **Preprocessor**: each `python` step has a matching `permissions.python` entry, the `.py` file exists, and the function is defined. In `mode: safe`, the AST is checked against the allowlist (no `open`, `eval`, `exec`, `__import__`, `subprocess`, etc.).

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | No errors (warnings may still be present) |
| `1` | One or more errors found |

## Output

Each issue is printed on its own line:

```
[error]   reyn/local/my_skill/phases/draft.md
          graph references unknown phase 'reveiw' (typo for 'review'?)

[warning] reyn/local/my_skill/phases/draft.md
          phase 'draft' has can_finish: true but no path to 'end'
```

A summary follows: `N error(s), M warning(s)`.

When clean: `No issues found.`

## Examples

Lint a project skill:

```bash
reyn lint article_writer
```

Lint a stdlib skill (sanity check after editing):

```bash
reyn lint eval
```

Use in CI:

```bash
reyn lint my_skill || exit 1
```

## See also

- [Reference: skill.md](../dsl/skill-md.md)
- [Reference: phase.md](../dsl/phase-md.md)
- [Reference: graph](../dsl/graph.md)
- [Reference: preprocessor](../dsl/preprocessor.md)
- [Reference: reyn skills](skills.md) — `reyn skills validate` for op/permission consistency (complements lint)
