---
type: reference
topic: stdlib
audience: [human, agent]
---

# Stdlib skills

Bundled skills shipped with reyn. Resolved last in name lookup (after `reyn/project/` and `reyn/local/`).

| Skill | Purpose |
|-------|---------|
| [skill_builder](skill_builder.md) | Generate a new skill from a natural-language description |
| [skill_improver](skill_improver.md) | Iteratively improve a skill against an eval spec |
| [skill_importer](skill_importer.md) | Import an external skill (e.g. Claude skill) into reyn |
| [eval](eval.md) | Evaluate one test case using LLM-as-judge |
| [eval_builder](eval_builder.md) | Generate an eval spec (`eval.md`) for a skill |
| [skill_router](skill_router.md) | Route a chat utterance to a skill (used by `reyn chat`) |
| [recall_memory](recall_memory.md) | Find memories relevant to a query |
| [write_memory](write_memory.md) | Extract and persist durable memories from a conversation |

Run `reyn skills <name>` for the full description and entry instructions of any skill.
