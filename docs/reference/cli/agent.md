---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn agent]
---

# `reyn agent`

Manage persistent agents — long-lived Session instances each with their own profile, history, memory layer, and inbox.

The auto-created `default` agent always exists; `reyn agent new` creates additional named agents. See [concepts/multi-agent](../../concepts/multi-agent/multi-agent.md) for the model.

## Synopsis

```
reyn agent <subcommand> [args]
```

Subcommands: `list`, `new`, `show`, `rm`.

## `reyn agent list`

Print all **active** (non-archived) agents (alphabetical), with last-activity timestamp and the first line of each profile's `role`. Archived agents are hidden from this listing. Pass `--all` to include archived agents, shown as `<name> (archived)`, so you can see / recover / purge them.

```bash
reyn agent list          # active agents only
reyn agent list --all    # also show archived agents
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

The `--role` text is injected into the agent's LLM system prompt — keep it short and specific. To configure `allowed_skills` or any other profile field, edit `profile.yaml` directly after creation; see [profile-yaml reference](../dsl/profile-yaml.md).

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

## `reyn agent rm <name> [--purge] [--yes]`

**Archive** the agent by default (soft-delete — data preserved, not destroyed). Pass `--purge` for a hard-delete that permanently destroys the agent directory and all rewind history.

```bash
reyn agent rm researcher            # archive (prompted)
reyn agent rm researcher --yes      # archive, skip prompt
reyn agent rm researcher --purge    # hard-delete (prompted, irreversible)
reyn agent rm researcher --purge --yes
```

The `default` agent cannot be removed.

### Archive (default)

The agent's `.reyn/agents/<name>/` directory is **kept in place** — the data is not destroyed. This is the key distinction from `--purge`:

- **PITR generations are preserved**: the WAL-derived checkpoint history survives, so the data is recoverable.
- **Topology membership is preserved**: no cascade fires. The agent's team/network membership is not removed.
- A tombstone marker is written recording the archival WAL seq (the WAL-window GC hinge).
- The agent is **hidden from active surfaces**: `reyn agent list`, the TUI Agents tab, default-topology routing, and A2A `can_send` checks all skip archived agents. It is dormant, not destroyed.

**WAL-window auto-purge**: when the WAL retention window advances past the archival seq, the archived agent's directory is hard-deleted automatically (the soft-delete left the rewind window — the data is no longer recoverable). At that point the topology cascade fires and removes the agent from all topologies.

### Purge (`--purge`)

Immediately hard-deletes `.reyn/agents/<name>/` and destroys all PITR generations. Time-travel to before the purge is intentionally unsupported. The topology cascade fires immediately (agent removed from all topologies; team topologies whose leader is purged are deleted entirely).

Use `--purge` when you want a clean, permanent delete and do not need the recovery window.

## Workspace layout

Each agent owns `.reyn/agents/<name>/`:

| Path | Purpose |
|------|---------|
| `profile.yaml` | name / role / created_at / allowed_skills |
| `history.jsonl` | append-only conversation + agent message log |
| `events.jsonl` | runtime event audit log |
| `memory/MEMORY.md` + body files | agent-scoped memory layer |
| `runs/<run_id>/` | per-skill-spawn workspace |

## See also

- [Reference: profile-yaml](../dsl/profile-yaml.md)
- [Reference: chat CLI](chat.md)
- [Reference: topology CLI](topology.md)
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [Concepts: time-travel](../../concepts/runtime/time-travel.md) — rewind + PITR mechanics
