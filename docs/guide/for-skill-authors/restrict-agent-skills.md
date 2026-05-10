---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [profile.yaml, allowed_skills]
---

# Restrict an agent's skill set

**Goal:** Limit which project / stdlib skills an agent's router can pick. Useful for specialist agents that should stay focused, and for production agents that shouldn't be allowed to invoke open-ended tools.

## When to use

- A `researcher` agent shouldn't ever invoke `article_writer`.
- An agent dedicated to a single workflow shouldn't be tempted (or hallucinate its way) into adjacent tools.
- You're nervous about what the router LLM might pick if it has 20 skills available.

## What this does NOT restrict

`allowed_skills` does **not** affect:

- **stdlib system skills** — `skill_router`, `chat_compactor`. These are always available; an agent with `allowed_skills: []` still chats. (FP-0011 removed the previous `skill_narrator` skill — the router LLM now narrates skill completions inline.)
- **agent-to-agent delegation** — `messages_to_agents` is governed by topology rules, not the skill allowlist. An agent with no skills can still delegate to a peer that has them.
- **memory access** — both shared and agent-scoped memory layers remain readable / writable.

## Recipe

`allowed_skills` lives in the agent's `profile.yaml`. There is no CLI flag yet (residual) — edit the file directly.

### 1. Find the file

```bash
$EDITOR .reyn/agents/researcher/profile.yaml
```

### 2. Add the field

```yaml
name: researcher
role: |
  deep technical research, prefers primary sources.
created_at: 2026-05-01T12:00:00+00:00
allowed_skills:
  - web_search
  - recall_docs
  - text_summarizer
```

Save the file. The next `reyn chat researcher` picks up the change at startup.

### 3. Verify

```bash
reyn agent show researcher
```

```
name:        researcher
created_at:  2026-05-01T12:00:00+00:00
workspace:   /path/to/project/.reyn/agents/researcher
allowed_skills:
  - web_search
  - recall_docs
  - text_summarizer
role:
  deep technical research, prefers primary sources.
```

## The three states

`allowed_skills` is tristate. Each state has distinct behavior:

| Value | Behavior |
|-------|----------|
| field absent / `null` | **Unrestricted.** Every project + stdlib skill is offered to the router LLM. (Default for new agents.) |
| `[]` (empty list) | **Router-only.** No skill spawn happens; the router can still reply directly or delegate to another agent. Useful for "pure conversational" agents. |
| `[a, b, c]` | **Allowlist.** Only those skill names are offered. |

Example for a deliberately conversational agent:

```yaml
name: lead
role: triages requests and synthesizes worker output.
allowed_skills: []  # never spawn skills directly; always delegate or reply
```

## Two-layer enforcement

1. **Router-side filter** — `_invoke_router` narrows `available_skills` to the allowlist before the LLM sees the catalogue. The LLM literally never knows about blocked skills.
2. **Defense in depth** — `_spawn_skill` re-checks at launch time. If the LLM emits a hallucinated skill name (or you tightened the allowlist mid-session), the spawn is refused with an error in the outbox.

The defense path is the one to inspect when something doesn't run as expected.

## Observing a refusal

If an LLM ever emits a `skills_to_run` entry not in the allowlist, the outbox shows:

```
[error] skill 'article_writer' is not in allowed_skills for agent 'researcher'; refused
```

and a structured event lands in `events.jsonl`:

```json
{"type": "skill_spawn_refused", "data": {"reason": "allowlist", "skill": "article_writer", "agent": "researcher"}}
```

Filter for refusals:

```bash
grep '"skill_spawn_refused"' .reyn/agents/researcher/events.jsonl
```

## Troubleshooting

**"The router LLM doesn't propose anything anymore."** Check the allowlist — `[]` is router-only by design. If you wanted a few skills, list them; if you wanted everything, remove the field entirely (or set it to `null`).

**"I get `not in allowed_skills` errors but the LLM still picks the same skill."** That's the defense-in-depth path. The router-side filter should normally hide blocked skills from the LLM, so reaching this branch means the LLM hallucinated the skill name. Tighten the role prompt to discourage it, or add the skill to the allowlist if you actually want it.

**"I added a skill to the allowlist but the router still doesn't pick it."** Confirm the skill exists in the catalogue:

```bash
reyn skills list
```

The allowlist filters an existing catalogue — names that don't resolve are silently dropped.

## See also

- [Reference: profile-yaml](../../reference/dsl/profile-yaml.md) — full schema and tristate semantics
- [Reference: agent CLI](../../reference/cli/agent.md)
- [Concepts: multi-agent](../../concepts/multi-agent.md) — where allowlist sits in the architecture
- [How-to: build an agent team](build-an-agent-team.md) — combine restriction with topology
