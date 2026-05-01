---
type: skill
name: skill_importer
description: |
  Search a public skills registry, let the user pick a candidate, and import
  the chosen skill as a multi-phase reyn skill under reyn/local/.
entry: search
final_output: skill_import_result
final_output_description: |
  The installed skill's path, the source URL it came from, and the list of
  phases the converter produced.
finish_criteria:
  - The user's query was used to fetch and filter candidates from the registry
  - The user picked one candidate (or only one matched and was auto-selected)
  - The chosen skill's source markdown was decomposed into one or more phases
  - skill.md / phases/ / artifacts/ files were written under reyn/local/<name>/
  - Provenance (imported_from, imported_at, imported_format) was embedded in skill.md
graph:
  search: [select]
  select: [convert]
  convert: []
routing:
  intents: [task]
  when_to_use:
    - User wants to import / install / pull an existing skill from a registry
    - User mentions importing from Anthropic skills, Claude skills, or similar
  when_not_to_use:
    - User wants to build a new skill from scratch (use skill_builder)
    - User wants to improve an installed skill (use skill_improver)
    - Conceptual questions about skill registries
  examples:
    positive:
      - "Anthropic の X skill を取り込んで"
      - "github の skill を import して"
      - "既存のスキルを reyn/local に追加して"
    negative:
      - "skill registry って何？"
      - "新しい skill を作って"   # this is skill_builder, not importer
---

## Overview

Discovers and imports skills from a public registry of Anthropic-style
skill markdown. The conversion is **LLM-driven**: a single source markdown
is decomposed into a multi-phase reyn skill, taking advantage of reyn's
phase graph instead of cramming everything into one phase.

## Input

`user_message` text — natural-language description of the capability you
want, e.g. "PDF を読んで要約する", "translate documents", "code review".

The user may also include the registry URL in the same message
("...from https://example.com/skills.md"). If no URL is present, the
search phase asks the user via `ask_user`.

## Output

`skill_import_result` with:

- `installed_path` — `reyn/local/<slug>/`
- `source_url` — the original markdown URL
- `phases` — list of phase names produced by the converter
- `imported_at` — ISO timestamp

## Provenance

The generated `skill.md` carries import metadata in its frontmatter:

```yaml
imported_from: <source_url>
imported_at:   <iso_timestamp>
imported_format: anthropic-skills/v1
imported_revision: <git_sha>   # only when the URL points to a known commit
```

Plus a `## Source` section in the body for human readers.

## Caveats

- The converter is best-effort. Skills with complex tool requirements,
  external API calls, or unusual schemas may need manual tuning after
  import. Re-running with updated phase instructions can help.
- A reyn `lint` is run on the result; failures are surfaced in the
  status output but the files are still written so the user can inspect
  and fix.
