---
type: agent
topic: architecture
audience: [agent]
---

# Agent-facing documentation

This directory holds documents whose **primary reader is a reyn skill**, not a human. Examples: planning checklists for `skill_builder`, mapping tables for `skill_importer`, criteria rubrics for `eval_builder`.

## Why a separate directory?

- **Predictable load paths.** When `recall_docs` (planned) is implemented, a calling skill can scope its retrieval to `docs/agent/` without sifting through human-targeted prose.
- **No translation overhead.** Agent instructions stay in English to avoid translation-introduced ambiguity.
- **Different writing style.** Checklists, rubrics, and lookup tables — not narrative prose.

## How a skill consumes these docs

For now, **transcribe** the relevant content into the skill's phase instructions (the δ approach). When `recall_docs` lands, the same content will become referenceable via:

```yaml
# skill_builder/phases/plan_skill.md (planned)
preprocessor:
  - run_skill:
      skill: recall_docs
      input: { type: doc_query, data: { topics: [dsl, preprocessor] } }
      into: relevant_docs
```

Until then, treat the contents of this directory as the **source of truth** for what skills should keep transcribed in their phase markdowns. When you change a doc here, update the matching phase markdowns to keep them in sync.

## Files

| File | Used by |
|------|---------|
| [glossary.md](glossary.md) | All skills (canonical term reference) |
| [skill-builder-checklist.md](skill-builder-checklist.md) | `skill_builder` |

(More files will be added as recurring agent-facing concerns surface — `skill-improver-criteria.md`, `skill-importer-mapping.md`, `eval-builder-rubric.md`.)

## See also

- [Concepts: principles](../en/concepts/principles.md) — the constraints these checklists enforce
- [Contributing: style-guide](../en/contributing/style-guide.md) — translation policy
