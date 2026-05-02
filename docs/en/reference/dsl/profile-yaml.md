---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [profile.yaml]
---

# `profile.yaml`

Per-agent metadata at `.reyn/agents/<name>/profile.yaml`. Created by `reyn agent new`; loaded on every `reyn chat` startup.

## Schema

```yaml
name: researcher                       # required (== directory name)
role: |                                # optional, default ""
  Deep technical research, prefers
  primary sources (arxiv, RFCs).
created_at: 2026-05-01T12:00:00+00:00  # ISO-8601 UTC, set by `reyn agent new`
allowed_skills:                        # optional, default null (unrestricted)
  - web_search
  - recall_docs
```

## Fields

### `name` (string, required)

Agent name. Must match `^[a-z0-9][a-z0-9_-]{0,31}$` and equal the parent directory name. The `default` agent is reserved and auto-created on first `reyn chat`.

### `role` (string, default `""`)

Free-form text injected into the agent's LLM system prompt as a `━━━ AGENT ROLE ━━━` block. Keep it short and behaviorally specific — this is what differentiates the agent from peers without changing skills.

Empty role is fine; the agent then behaves like a generalist with no extra persona.

### `created_at` (string, default `""`)

ISO-8601 UTC timestamp set when `reyn agent new` runs. Cosmetic; not consulted at runtime.

### `allowed_skills` (`list[str]` | `null`, default `null`)

Skill allowlist. Three states with distinct meaning:

| Value | Meaning |
|-------|---------|
| absent / `null` | **Unrestricted.** Every project + stdlib skill is offered to the router LLM. |
| `[]` (empty list) | **Router-only.** No skill spawn happens; the router can still reply directly or delegate to another agent. |
| `[a, b, c]` | **Allowlist.** Only the listed skill names are offered. |

Stdlib router (`skill_router`), compactor (`chat_compactor`), and narrator (`skill_narrator`) are **always** enabled and are not subject to this list — they're system skills, not agent-selectable.

Two-layer enforcement:

1. **Router-side filter** — `_invoke_router` narrows `available_skills` to the allowlist before the LLM sees the catalogue.
2. **Defense in depth** — `_spawn_skill` re-checks at launch time. A blocked spawn surfaces an `error` in the outbox and a `skill_spawn_refused` event with `reason="allowlist"`.

## Editing

`reyn agent new --role` writes a fresh profile. To change `allowed_skills` (or any other field) afterwards, edit the file directly — there is no `reyn agent set-skills` CLI yet (residual). The format is permissive about ordering and trailing keys.

## See also

- [Reference: agent CLI](../cli/agent.md)
- [Concepts: multi-agent](../../concepts/multi-agent.md)
- [Reference: skill_router](../stdlib/skill_router.md) — how `available_skills` reaches the LLM
