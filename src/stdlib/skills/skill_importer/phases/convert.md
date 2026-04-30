---
type: phase
name: convert
input: selected_candidate
role: skill_converter
can_finish: true
max_act_turns: 6
permissions:
  file.write:
    - path: reyn/local
      scope: recursive
---

Fetch the chosen source markdown, decompose it into a multi-phase reyn
skill, write the files, lint the result, and report back.

## Step 1 — Fetch the source

In one act turn, emit a `web_fetch` op for `selected_candidate.data.source_url`.

The body of the fetched markdown is the skill's instructions. Some sources
also have YAML frontmatter (`---` block at the top) with `name`,
`description`, etc. — extract those if present.

## Step 2 — Decompose

Read the source carefully. Most Anthropic-style skills are written as a
single block of instructions; your job is to split that into reasonable
**phases**. Look for natural boundaries:

- Setup / preparation steps
- Main work / generation
- Review / validation / refinement
- Final formatting / output

Aim for **2–4 phases**. A single-phase skill is fine if the source is
genuinely simple. Don't manufacture phases that aren't in the source.

For each phase, decide:

- `name` — short snake_case (e.g. `analyze`, `draft`, `review`)
- `instructions` — the prose for that phase, lifted/condensed from the source
- `input` — usually the previous phase's output artifact, or `user_message`
  for the entry phase
- `can_finish` — true only on the last phase

## Step 3 — Decide skill identity

From the source's frontmatter (or the registry candidate name) derive:

- **Slug** — lowercase snake_case, e.g. `pdf_summarizer`, `code_reviewer`.
  Used as the directory name and the skill `name:` field.
- **Description** — one line, in the user's language if the source had it
  in that language.
- **Final output** — typically a `text` or `result` artifact. If the source
  produces structured data, declare a per-skill artifact for it; otherwise
  reuse `user_message` as a passthrough wrapper.

## Step 4 — Write the files

In one act turn, emit `file` ops with `op: write` for:

### `reyn/local/<slug>/skill.md`

```yaml
---
type: skill
name: <slug>
description: <one-line>
entry: <first_phase_name>
final_output: <output_artifact_name>
final_output_description: <one-line>
finish_criteria:
  - <criterion 1>
  - <criterion 2>
graph:
  <phase_a>: [<phase_b>]
  <phase_b>: []
imported_from: <source_url>
imported_at: <iso_timestamp_utc>
imported_format: anthropic-skills/v1
---

## Overview

<one or two paragraphs lifted from the source>

## Source

This skill was imported by `skill_importer` on <iso_date> from:
<<source_url>>

To re-import the latest version, run:

    reyn run skill_importer "<query>"
```

The `imported_*` keys MUST appear in the frontmatter. They are inert metadata
the parser ignores; they exist so the user can see and the future sync skill
can find the upstream.

If the URL contains a git commit SHA (e.g.
`raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>`), also include
`imported_revision: <sha>`.

### `reyn/local/<slug>/phases/<phase_name>.md` (per phase)

```yaml
---
type: phase
name: <phase_name>
input: <input_artifact_name>
role: <optional_role>
can_finish: <true|false>
---

<phase instructions, lifted/condensed from the source>
```

### `reyn/local/<slug>/artifacts/<art>.yaml` (only if needed)

If you defined a custom artifact in Step 3, write its YAML schema here.
For passthrough skills using only `user_message`, no artifact files are
needed (stdlib provides `user_message`).

## Step 5 — Lint the result

In one act turn, emit a `lint` op:

```json
{"kind": "lint", "skill_path": "reyn/local/<slug>"}
```

Capture `passed`, `error_count`, and any issues in the result.

## Step 6 — Decide turn

Emit `skill_import_result` with:

- `installed_path`: `reyn/local/<slug>`
- `skill_name`: `<slug>`
- `source_url`: from `selected_candidate`
- `imported_at`: the same UTC ISO timestamp you wrote into the frontmatter
- `phases`: list of phase names you created (entry first)
- `lint_passed`: result of Step 5
- `notes`: any caveats — fields skipped, ambiguous decomposition, lint
  warnings the user should address, etc.

control.type = "finish".

## Constraints

- **One web_fetch** for the source. Don't fetch other URLs.
- Write only under `reyn/local/<slug>/` — never outside that directory.
- The `imported_at` value MUST be UTC and ISO 8601, e.g.
  `2026-04-30T05:42:18Z`. Use the time you start the convert phase, not
  the original source's date.
- Do NOT include credentials, API keys, or anything else in the
  imported_from URL. If the source URL contained query-string secrets,
  strip them before recording (this should never happen for public
  registries but be defensive).
- If the source is empty, malformed, or clearly not a skill, abort with
  control.type='abort' and a summary explaining what was wrong.
