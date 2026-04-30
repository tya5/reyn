---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# `skill.md` frontmatter

Every skill is a directory containing a `skill.md` whose YAML frontmatter declares the skill's structure.

## Schema

```yaml
---
type: skill                    # always "skill"
name: my_skill                 # unique identifier
description: One-line summary  # shown in `reyn skills`
entry: <phase_name>            # required; phase that runs first
final_output: <artifact_type>  # required; schema for the skill's result
final_output_description: |    # optional; human-readable result description
  ...
finish_criteria:               # optional; conditions for clean termination
  - All inputs validated
  - Final output passes the quality bar
graph:                         # required; allowed transitions
  outline: [expand]
  expand: [end]
permissions:                   # optional; declares required capabilities
  shell: deny
  python:
    - module: stats
      function: compute
      mode: pure
imported_from: ...              # optional; provenance, set by skill_importer
imported_at: 2026-04-29T...
imported_format: claude-skill
imported_revision: <git-sha>
---
```

## Required fields

- **`type`** — must be `skill`.
- **`name`** — used for resolution and event correlation.
- **`entry`** — name of the phase to start with. Must exist in `phases/`.
- **`final_output`** — artifact type produced when the skill finishes. Must be defined in `artifacts/<name>.yaml` or be a stdlib artifact.
- **`graph`** — adjacency list. Each key is a phase name; each value is a list of allowed next-phase names. Use `end` to mark terminal transitions.

## Optional fields

- **`description`** — appears in `reyn skills`.
- **`final_output_description`** — long-form description shown in skill detail.
- **`finish_criteria`** — used by phases to know when finishing is allowed.
- **`permissions`** — see `reference/config/permissions.md` (Phase 2).
- **`imported_*`** — provenance fields written by `skill_importer`. Inert; the parser ignores them.

## Body

After the frontmatter, the markdown body is the skill's prose description: what it does, when to use it, examples. Shown by `reyn skills <name>`.

## Validation

`reyn lint <skill_name>` checks:

- All phases referenced in `graph` exist in `phases/`.
- `entry` is a key in `graph`.
- `final_output` matches an artifact in `artifacts/` or stdlib.
- Phase artifact references are resolvable.
- Python preprocessor steps (if any) match `permissions.python` and have a corresponding `.py` file.

## Example

```yaml
---
type: skill
name: my_explainer
description: Generate a one-paragraph explainer from a topic.
entry: outline
final_output: explainer
graph:
  outline: [expand]
  expand: [end]
---

# my_explainer

Takes a `topic_input` artifact and produces a friendly, example-rich
one-paragraph explainer. Two phases: `outline` produces 3 bullets;
`expand` turns them into prose.
```

## See also

- [phase-md.md](phase-md.md) — Phase frontmatter
- `reference/dsl/artifact-yaml.md` — artifact schema files (Phase 2)
- `reference/dsl/graph.md` — graph semantics in depth (Phase 2)
- [Concepts: P2 Skill defines structure](../../concepts/principles.md#p2-skill-defines-structure)
