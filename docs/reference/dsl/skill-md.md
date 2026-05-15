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
      mode: safe
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
- **`permissions`** — see [`permissions:` (skill-level)](#permissions-skill-level) below.
- **`postprocessor`** — see [`postprocessor:`](#postprocessor) below.
- **`imported_*`** — provenance fields written by `skill_importer`. Inert; the parser ignores them.
- **`search_hints`** — optional; list of example query strings this skill can answer. Used by the BM25/embedding pre-filter when the catalog exceeds the router context window. Set by skill authors to improve recall in large multi-skill repos.
  Example: `search_hints: ["summarize an article", "tl;dr a document"]`

## `permissions:` (skill-level)

`permissions:` in `skill.md` frontmatter is the **only** location for permission declarations. Phase-level permissions were removed in the skill-only permissions migration. See [permission-model.md](../../concepts/permission-model.md) for full semantics and capability hierarchy.

```yaml
permissions:
  shell: deny                 # deny | allow; default deny
  file.read: allow            # allow read access to workspace files
  file.write: allow           # allow write access to workspace files
  mcp:
    - server: github
      ops: [read]             # allowlist of op kinds for this MCP server
    - server: jira
      ops: [read, write]
  python:
    - module: stats           # module name (no .py extension)
      function: compute
      mode: safe              # safe | unsafe
    - module: rendering
      function: to_html
      mode: unsafe            # requires --allow-untrusted-python flag
  tool:
    - name: web_search        # Control IR tool name
      allow: true
```

### Key fields

- **`shell`** — `allow` or `deny` (default `deny`). Governs whether Control IR `shell` ops are accepted.
- **`file.read`** / **`file.write`** — workspace file access. Default: `file.read: allow`, `file.write: deny`.
- **`mcp`** — list of MCP server entries. Each entry names a server and an `ops` allowlist. Ops not in the list are rejected at dispatch.
- **`python`** — list of Python function entries allowed in preprocessor and postprocessor `python` steps. Each entry must match the `module` + `function` pair used in a step. `mode: safe` runs sandboxed; `mode: unsafe` requires `--allow-untrusted-python` at the CLI.
- **`tool`** — list of named Control IR tools the skill may invoke.

The `permissions` block is the upper-bound gate: even if a phase's `allowed_ops` would permit an op, the op is rejected at dispatch if it falls outside `skill.permissions`. See [permission-model.md](../../concepts/permission-model.md) for the layered enforcement model.

## `postprocessor:`

A skill may optionally declare a `postprocessor` block — a deterministic transformation that runs at skill finish, between the LLM's final output and the artifact returned to the caller.

```yaml
postprocessor:
  output_schema: rendered_post   # artifact-name string OR inline dict
  output_description: |
    Fully rendered HTML post with word count.
  steps:
    - type: python
      module: rendering
      function: to_html
      into: html_body
    - type: validate
      schema:
        type: object
        required: [html_body]
        properties:
          html_body: { type: string }
```

For full syntax — required fields, optional fields, step kinds, `on_error` policy, and permission gate — see [postprocessor.md](postprocessor.md). For rationale, see [Concepts: postprocessor](../../concepts/postprocessor.md).

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
