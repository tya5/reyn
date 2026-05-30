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

> Note: the previous `skill_narrator` stdlib skill has been removed.
> (2026-05-10). The router LLM now narrates skill completions inline as part
> of its post-`invoke_skill` turn — see the router system prompt's
> For "spawn-ack + completion-narration" guidance and
> async-dispatch flow that landed alongside.
>
> The `chat_compactor` stdlib skill has been retired. Chat history
> compaction is now handled by OS-internal Python (`CompactionEngine` at
> `reyn.services.compaction.engine`) with no phase-frame overhead.

Run `reyn skills <name>` for the full description and entry instructions of any skill.
