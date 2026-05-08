---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn topology]
---

# `reyn topology`

Manage agent communication topologies — declarative structure that constrains which agent can send to which.

Three kinds are supported: `network` (complete graph), `team` (leader-centric star), `pipeline` (directed path). The auto-managed `_default` network covers every agent that does NOT belong to any user-declared topology — the empty-state case behaves freely while declared topologies enforce their rules immediately. See [concepts/topology](../../concepts/topology.md) for the model.

## Synopsis

```
reyn topology <subcommand> [args]
```

Subcommands: `list`, `new`, `show`, `rm`, `add-member`, `rm-member`.

## `reyn topology list`

Print user-declared topologies first (alphabetical), with `_default` last.

```bash
reyn topology list
```

```
NAME      KIND      MEMBERS
team1     team      default*, alpha
_default  network   beta, gamma
```

`*` marks the leader of a `team` kind. `_default` is auto-managed; its membership = every agent not in any other topology.

## `reyn topology new <name> --kind KIND --members A,B,C [--leader LEADER]`

Create a user-declared topology under `.reyn/topologies/<name>.yaml`.

```bash
reyn topology new team_research --kind team \
    --members default,researcher,writer --leader default

reyn topology new pipe_publish --kind pipeline \
    --members researcher,editor,publisher
```

Validation:

- `<name>` must match `[a-z0-9][a-z0-9_-]{0,31}` and is NOT `default` (reserved). The `_` prefix is rejected by the regex, so `_default` cannot be created either.
- `--members` agents must already exist (`reyn agent list`).
- `--kind team` requires `--leader` AND the leader must be in `--members`.
- `--kind pipeline` keeps `--members` order significant: edges flow `members[i] → members[i+1]` only.
- `--kind network` accepts any order; all member-pairs in both directions are permitted.

## `reyn topology show <name>`

Print the topology and the full set of permitted directed edges:

```bash
reyn topology show team_research
```

```
name:        team_research
kind:        team
leader:      default
members:     default*, researcher, writer
created_at:  2026-05-01T12:00:00+00:00

permitted edges (4):
  default → researcher
  default → writer
  researcher → default
  writer → default
```

`reyn topology show _default` works too and is annotated as auto-managed.

## `reyn topology rm <name> [--yes]`

Delete a user-declared topology. `_default` cannot be removed; attempting it surfaces a clear error.

```bash
reyn topology rm team_research --yes
```

## `reyn topology add-member <topology> <agent>`

Append `agent` to `members`. For `pipeline` kind, the new member becomes the new tail.

```bash
reyn topology add-member team_research editor
```

Mutating `_default` directly is rejected — its membership is computed from "agents not in any other topology" and adjusts automatically as user-declared topologies change.

## `reyn topology rm-member <topology> <agent>`

Remove `agent` from `members`. For `team` kind, removing the leader is rejected (delete the topology instead). After removal, if the agent ends up in no user-declared topology, it returns to `_default`.

```bash
reyn topology rm-member team_research editor
```

## Cascade from `reyn agent rm`

Removing an agent via `reyn agent rm` automatically drops it from every topology it was a member of. A `team` whose leader is removed is deleted entirely. Empty topologies are also deleted.

## See also

- [Concepts: topology](../../concepts/topology.md) — kind semantics, `_default`, permit rules
- [Reference: topology-yaml](../dsl/topology-yaml.md) — on-disk schema
- [Reference: agent CLI](agent.md)
