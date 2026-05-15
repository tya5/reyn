---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn skills]
---

# `reyn skills`

List installed skills, show usage details, or validate op/permission cross-layer consistency.

## Synopsis

```
reyn skills
reyn skills <SKILL_NAME>
reyn skills validate <SKILL_NAME>
reyn skills validate --all
```

## Subcommands / forms

| Form | Description |
|------|-------------|
| `reyn skills` | List all installed skills (project → local → stdlib). |
| `reyn skills <SKILL_NAME>` | Print usage details for one skill — description, entry phase, final output, and body. |
| `reyn skills validate <SKILL_NAME>` | Validate op/permission cross-layer consistency for one skill (FP-0026). |
| `reyn skills validate --all` | Validate all installed skills and print a summary. |

## `reyn skills` — listing

Prints a table of all installed skills across project (`reyn/project/`), local (`reyn/local/`), and stdlib skill directories. Columns: name, source, one-line description.

```
NAME                SOURCE    DESCRIPTION
text_summarizer     stdlib    Summarise text into a compact paragraph
article_writer      project   Draft and review a long-form article
```

## `reyn skills <SKILL_NAME>` — detail

Prints full usage information for one skill:

```
skill: article_writer (project)
entry:        draft
final_output: article

[body / description from skill.md]
```

Resolution order: `reyn/project/` → `reyn/local/` → stdlib.

## `reyn skills validate` — op/permission consistency check (FP-0026)

Checks that every **Tier 2-3 op** a skill's phases declare in `allowed_ops` has a matching entry in `skill.permissions`, and conversely that every declared permission is actually referenced by at least one phase.

What is checked:

- **Undeclared permission**: a phase lists a Tier 2-3 op kind (e.g. `shell`, `mcp`) in `allowed_ops` but the skill's `permissions:` block has no entry for it → **error**.
- **Dead permission**: the skill declares a permission but no phase references the op kind → **warning**.

Tier 0-1 ops (`ask_user`, `run_skill`, `web_search`, `web_fetch`, etc.) are exempt — they require no declaration.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | No errors (warnings may still be present). |
| `1` | One or more errors, or skill not found. |

### Output

Single-skill run:

```
Skill 'my_skill': OK — no cross-layer inconsistencies.
```

With issues:

```
[error]   my_skill
          phase 'draft' has allowed_ops=[shell] but skill.permissions has no 'shell' entry

[warning] my_skill
          skill.permissions declares 'mcp:[github]' but no phase lists 'mcp' in allowed_ops
```

Validate-all summary:

```
Validated 12 skill(s). 1 error(s) in 1 skill(s), 2 warning(s) in 2 skill(s).
```

### Examples

```bash
# Validate one skill
reyn skills validate article_writer

# Validate every installed skill (useful in CI)
reyn skills validate --all || exit 1
```

## See also

- [Reference: skill.md](../dsl/skill-md.md) — `permissions:` and `allowed_ops` fields
- [Reference: phase.md](../dsl/phase-md.md) — `allowed_ops` field
- [Reference: lint](lint.md) — `reyn lint` (graph + artifact checks; complementary)
- [Concepts: permission-model](../../concepts/permission-model.md) — Tier 0-3 op classification
