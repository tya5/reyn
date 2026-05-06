---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_importer]
---

# `skill_importer`

Search a public skills registry, let the user pick a candidate, and import the chosen skill as a multi-phase reyn skill under `reyn/local/`.

## Entry

`search`

## Final output

`skill_import_result` — installed path, source URL, and the list of files written.

## Provenance

The imported skill's `skill.md` carries `imported_from`, `imported_at`, `imported_format`, and `imported_revision` fields. These are inert — the parser ignores them — but they let you trace back to the source.

## Example

```bash
reyn run skill_importer "find a markdown summarizer skill"
```

## Source

[`src/stdlib/skills/skill_importer/skill.md`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/skill_importer/skill.md)
