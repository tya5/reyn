---
type: concept
topic: integration
audience: [human, agent]
---

# Skills

A skill is a reusable, task-specific instruction set — an industry-standard `SKILL.md` file (YAML frontmatter + Markdown body) that tells the model *when* a technique applies and *how* to carry it out. Skills are registered explicitly, exposed to the model as a lightweight menu, and read on demand — the same layered-disclosure shape as MCP tools, applied to instructions instead of APIs.

This is a different mechanism from the pre-1.0 `skill.md`-driven phase-graph workflow engine (removed; see the multi-agent / control-IR docs for the current execution model). A "skill" here is closer to a Claude Skill: a folder with instructions the model chooses to read, not a program the OS executes.

## Registration: explicit entries, no directory scan

Skills are registered purely via `skills.entries` declarations in config — the same model as `mcp.servers`. There is no directory scan; a `SKILL.md` file sitting on disk with no config entry is invisible to every session.

```yaml
# reyn.yaml
skills:
  entries:
    pdf_editing:
      path: skills/pdf-editing/SKILL.md
      description: "Fill, merge, and extract fields from PDF forms"
      enabled: true
      auto_invoke: true
```

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `path` | string | required | Path to `SKILL.md` (or its containing directory). Project-root-relative or absolute. |
| `description` | string | `""` | One-line summary shown in the L1 menu. Truncated to the first line, capped at 200 characters. |
| `enabled` | bool | `true` | `false` removes the entry from the registry entirely (not just hidden). |
| `auto_invoke` | bool | `true` | `false` keeps the skill registered but excludes it from the L1 system-prompt menu — the model won't be told it exists unless something else surfaces it. |

The registry never reads `SKILL.md` itself — only `path` and `description` from the config entry populate the L1 menu. The file is read by the model at L2, on demand, via the ordinary file-read op.

## Config cascade

`skills.entries` merges across the same tiers as every other config section, later tiers winning on name collision:

1. `~/.reyn/config.yaml` — user-global
2. `reyn.yaml` — project
3. `reyn.local.yaml` — project-local (gitignored)
4. `.reyn/config/skills.yaml` — runtime-dynamic, written by the `skill_management__install_*` tools

Hand-editing any of the first three is a normal way to register a skill; the fourth is written automatically by the install tools below and reflects what a session installed for itself.

## Writing a `SKILL.md`

```markdown
---
name: pdf-editing
description: Fill, merge, and extract fields from PDF forms
---

# PDF editing

Use `pypdf` for form-field operations...
```

`name` and `description` are frontmatter keys read by the install tools (see below) to prefill a `skills.yaml` entry — the config entry's own `description` is what actually reaches the model, so keep it accurate and short (first line only; longer detail belongs in the body). The Markdown body is free-form: this is model-facing instruction text, not a schema the OS parses.

## Three-layer exposure

| Layer | What the model sees | Mechanism |
|-------|---------------------|-----------|
| **L1 — menu** | A dedicated `## Skills` system-prompt block, one line per enabled + auto-invoke skill: `name — description [path]`. | Built once per turn from the registry; no dedicated dispatch. |
| **L2 — instructions** | The full `SKILL.md` body, read only when the model judges the current task matches an entry's description. | Ordinary `file__read` — no dedicated "invoke skill" op. |
| **L3 — bundled assets** | Any additional files the skill's instructions reference (templates, scripts, reference data) sitting alongside `SKILL.md`. | Ordinary `file__read`, gated by the standard permission model like any other path. |

There is no dedicated "run this skill" primitive at any layer — a skill is discovered via L1, loaded via L2, and its assets are just files. The model decides relevance from the L1 description; the OS does not gate *which* skill the model may read, only *which paths* it may read (the standard permission model — reading inside the project root is a default; outside requires the usual declaration + approval).

## Hot-reload

Edits to `.reyn/config/skills.yaml` take effect at the next turn boundary via the `"skills"` reload seam — no session restart needed. Editing `reyn.yaml` / `reyn.local.yaml` directly follows the same general config hot-reload path as other sections; see [Concepts: Config hot-reload](../runtime/config-hot-reload.md).

## Per-session visibility toggle

A skill can be hidden from a single session without touching config, via the same status-bar-style visibility override used for tools / MCP servers / categories: `set_capability_visible("skill", name, visible)`. This is **restrict-only** — toggling a skill name that isn't in the registered set (or that a topology/delegation envelope already denies) is a silent no-op; visibility can never grant access beyond what's registered.

## Installing skills

Two chat-callable tools under the `skill_management` category write `skills.yaml` entries — there is no `reyn skill` CLI equivalent in v1/v2 (skill management is a chat-driven, in-conversation flow).

### `skill_management__install_local`

Registers a local skill directory (or a direct path to its `SKILL.md`) into `.reyn/config/skills.yaml`:

1. Resolves `SKILL.md` (directory → `<dir>/SKILL.md`, or a direct file path).
2. Reads `name` / `description` from frontmatter (`name` override argument takes precedence; falls back to the directory basename if frontmatter has none).
3. Threat-scans the description (strict scope) — blocks on a blocking-severity match.
4. Gates the `skills.yaml` write through the standard `require_file_write` permission flow.
5. Writes the entry, records a config generation (crash-recovery — survives WAL truncation), emits a `skill_installed` P6 event, and requests a hot-reload.

### `skill_management__install_source`

Fetches a skill from a git/GitHub URL and installs the clone:

1. Gates `require_http_get` for the source host.
2. Shallow-clones the repo (`--depth 1`) to `.reyn/skills/<name>/`. A `//subdir` suffix on the URL (mirroring Terraform's module-subdir convention) selects a subdirectory of the clone instead of its root.
3. Locates `SKILL.md` in the clone, then proceeds through the same frontmatter-read → threat-scan → gate → write → hot-reload pipeline as the local path, with the registered `path` pointing at the installed copy.

**Path-safety hardening** (both tools, since the resolved name feeds a filesystem path under `.reyn/skills/`): the derived name — from the `name` argument, `SKILL.md` frontmatter, or a URL/subdir basename — is rejected outright unless it is a single safe path component (`[A-Za-z0-9._-]+`, no `..`, no leading dot, no separators). A belt-and-suspenders containment check (`resolve()` + `relative_to()`) additionally refuses any install destination that would resolve outside `.reyn/skills/`, guarding against a gap in the name check itself. Neither check silently rewrites an unsafe name — installation is refused with an explicit error instead.

## What's out of scope (for now)

Deliberately not part of the current model — planned for a future layer, not a gap in this one:

- Per-skill tool-permission scoping (an `allowed-tools` style activation scope)
- Dynamic shell-command execution syntax inside skill instructions
- A marketplace / registry index for discovering skills (unlike MCP's official registry)
- `list_skills` / `describe_skill` introspection tools or CLI

## See also

- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `skills:` block schema
- [Concepts: MCP](mcp.md) — the analogous external-capability registration model
- [Concepts: permission model](../runtime/permission-model.md) — the file-read/file-write gates skills use
- [Concepts: Config hot-reload](../runtime/config-hot-reload.md) — the general reload cycle
