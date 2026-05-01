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
| [skill_router](skill_router.md) | Route a chat utterance to a skill, peer agent, or direct reply (used by `reyn chat`). Reads + writes memory inline. |
| skill_narrator | Narrate the result of a finished skill spawn back into the chat history. Spawned automatically by `reyn chat` when a skill completes; not directly invokable. |
| chat_compactor | Compact a long chat history into a structured rolling summary. Spawned automatically by `reyn chat` when token thresholds trigger; not directly invokable. |

Run `reyn skills <name>` for the full description and entry instructions of any skill.
