---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn agent]
---

# `reyn agent`

Manage persistent agents — long-lived ChatSession instances each with their own profile, history, memory layer, and inbox.

The auto-created `default` agent always exists; `reyn agent new` creates additional named agents. See [concepts/multi-agent](../../concepts/multi-agent.md) for the model.

## Synopsis

```
reyn agent <subcommand> [args]
```

Subcommands: `list`, `new`, `show`, `rm`.

## `reyn agent list`

Print all known agents (alphabetical), with last-activity timestamp and the first line of each profile's `role`.

```bash
reyn agent list
```

```
NAME        LAST ACTIVITY     ROLE
default     2026-05-01 13:00  
researcher  2026-05-01 12:55  deep technical research, prefers primary sources
writer      2026-04-30 18:20  concise long-form prose
```

## `reyn agent new <name> [--role TEXT]`

Create a new agent under `.reyn/agents/<name>/`. The directory is provisioned with a `profile.yaml`; `history.jsonl`, `events.jsonl`, `memory/`, and `runs/` are created on first activity.

```bash
reyn agent new researcher --role "deep technical research, prefers primary sources"
```

`<name>` must match the agent name regex: 1–32 characters of `[a-z0-9_-]` starting with `[a-z0-9]`.

The `--role` text is injected into the agent's LLM system prompt (PR10) — keep it short and specific. To configure `allowed_skills` or any other profile field, edit `profile.yaml` directly after creation; see [profile-yaml reference](../dsl/profile-yaml.md).

## `reyn agent show <name>`

Print profile metadata and resolved fields:

```bash
reyn agent show researcher
```

```
name:        researcher
created_at:  2026-05-01T12:00:00+00:00
workspace:   /path/to/project/.reyn/agents/researcher
allowed_skills: (unrestricted — all project + stdlib skills)
role:
  deep technical research, prefers primary sources
```

`allowed_skills` renders as one of:

- `(unrestricted — all project + stdlib skills)` — field absent / `null`
- `(none — router-only, no skill spawn)` — empty list `[]`
- bullet list — populated allowlist

## `reyn agent rm <name> [--yes]`

Delete the agent's directory recursively (history, events, memory layer, runs). Cascades through any user-declared topology that listed this agent as a member; team topologies whose leader is being removed are deleted entirely.

```bash
reyn agent rm researcher --yes
```

The `default` agent cannot be removed.

## Workspace layout

Each agent owns `.reyn/agents/<name>/`:

| Path | Purpose |
|------|---------|
| `profile.yaml` | name / role / created_at / allowed_skills |
| `history.jsonl` | append-only conversation + agent message log |
| `events.jsonl` | runtime event audit log |
| `memory/MEMORY.md` + body files | agent-scoped memory layer (PR15) |
| `runs/<run_id>/` | per-skill-spawn workspace |

## See also

- [Reference: profile-yaml](../dsl/profile-yaml.md)
- [Reference: chat CLI](chat.md)
- [Reference: topology CLI](topology.md)
- [Concepts: multi-agent](../../concepts/multi-agent.md)
