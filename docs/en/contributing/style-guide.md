---
type: reference
topic: architecture
audience: [human]
---

# Documentation style guide

Rules for writing docs that humans and reyn skills can both rely on.

## Frontmatter is required

Every page under `docs/` MUST start with YAML frontmatter:

```yaml
---
type: tutorial | how-to | reference | concept | adr | agent | landing
topic: dsl | runtime | cli | stdlib | config | architecture | getting-started
applies_to: [skill.md, phase.md]   # optional; specific files/commands this doc covers
audience: [human, agent]            # who is the primary reader
---
```

The `audience` tag is what eventually lets `recall_docs` filter relevant content for a calling skill — keep it accurate.

## File granularity: one concept per file

agents (and humans skimming) should be able to load exactly the concept they need. Don't pile multiple unrelated concepts into one long file. If a `reference/` page exceeds ~300 lines, consider splitting.

## Diátaxis discipline

Each of the four reading modes has a different job. Don't mix them inside a single document.

| Mode | Job | What NOT to do |
|------|-----|----------------|
| Tutorial | Teach by doing | No exhaustive option lists |
| How-to | Solve one problem | No conceptual digressions |
| Reference | Be accurate and complete | No tutorial-style narrative |
| Concept | Explain the *why* | No step-by-step instructions |

If a tutorial wants to explain *why* something works, link to a concept page instead of explaining inline.

## Cross-linking

- Use **relative links** within `docs/` (e.g. `../reference/dsl/skill-md.md`).
- Link forward and backward: a tutorial points to relevant reference; a reference page links to the concept that explains it.
- Don't link to source files except from `reference/stdlib/`.

## Code examples

- Every reference example MUST be runnable as shown — copy-paste should work.
- Tutorial code MAY be partial, but mark omissions explicitly (`# ... rest of file`).

## Translation policy (en ↔ ja)

- **Primary language is English.** Write the en version first.
- **ja is fallback-friendly.** `mkdocs-static-i18n` falls back to en for missing files. Translate when ready; don't block en updates on ja.
- **agent/ is en-only.** Avoid translating agent-facing instructions to keep ambiguity low.
- **changelog/ADR/contributing are en-only.** Operational docs.
- **Sync when feasible, but don't gate.** If you change an en page, leave a TODO note on the ja page or open an issue.

When translating:
- Keep code blocks identical (don't translate variable names, file paths, CLI flags).
- Translate prose and headings.
- Keep the frontmatter `topic`/`type` values in English (they're machine-readable tags).
- Add a `lang: ja` field if helpful, though not required.

## Glossary

The DSL/runtime terms are listed in [`docs/en/guide/for-skill-authors/glossary.md`](../guide/for-skill-authors/glossary.md). When writing in either language, use the same canonical term names from that glossary so cross-language cross-references stay consistent.

## When you change runtime/DSL semantics

Update **all three**:

1. The relevant `reference/` page.
2. The relevant `concepts/` page if the *why* changed.
3. `CLAUDE.md` if it documents the same constraint for code-writing agents.

A drift between these three is what `reference/` is meant to prevent — but only if you keep them in sync.

## Build and lint locally before committing

```bash
make docs-build       # strict mode — fails on broken links / config issues
```
