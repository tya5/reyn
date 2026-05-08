---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [topology.yaml]
---

# `topology.yaml`

Declared communication topology at `.reyn/topologies/<name>.yaml`. Created by `reyn topology new`. Loaded by `AgentRegistry` on every process start.

The auto-managed `_default` network topology is **not** stored on disk — it lives only in memory and is computed from "agents not in any user-declared topology". See [concepts/topology](../../concepts/topology.md).

## Schema

```yaml
name: team_research                       # required
kind: team                                # required: "network" | "team" | "pipeline"
members:                                  # required, ordered for kind=pipeline
  - default
  - researcher
  - writer
leader: default                           # required iff kind=team, must be in members
created_at: 2026-05-01T12:00:00+00:00     # ISO-8601 UTC, set by `reyn topology new`
```

## Fields

### `name` (string, required)

Topology name. Must match `^[a-z0-9][a-z0-9_-]{0,31}$`. The names `default` and `_default` are reserved.

### `kind` (string, required)

One of:

- `network` — complete graph among `members`. `can_send(A, B) = (A != B and A,B ∈ members)`.
- `team` — star around `leader`. `can_send(A, B) = (leader ∈ {A, B} and A != B and A,B ∈ members)`. Peer-to-peer (member ↔ member, neither being the leader) is forbidden.
- `pipeline` — directed path. `can_send(A, B) = members.index(B) == members.index(A) + 1`. No jumps, no reverse, no fan-out.

`tree`, `meeting`, `pair`, `broadcast` kinds are **not** implemented — `tree` is expressible as overlapping `team` topologies (see [concepts/topology](../../concepts/topology.md#tree-pattern)), the rest are residuals waiting on demand.

### `members` (list of strings, required)

Names of agents that participate. Order is **significant** for `kind: pipeline` (defines the directed path); informational for `kind: network` and `kind: team`. Each name must reference an existing agent at the time of `reyn topology new` / `add-member`; cascade in `reyn agent rm` automatically prunes references.

`team` requires at least one member who matches `leader`. `pipeline` rejects duplicate members (would create a cycle). `network` rejects empty `members` lists (no edges to permit).

### `leader` (string, required for `kind: team`)

Agent name of the team's leader. Must appear in `members`. Must NOT be set for `kind: network` or `kind: pipeline`.

### `created_at` (string, default `""`)

ISO-8601 UTC timestamp set when `reyn topology new` runs. Cosmetic.

## Permit rule (registry-level)

The registry's `permit(A, B)` checks every topology containing both `A` and `B` as members and returns True if **any** of them permits the edge via its `can_send`. There is no permissive fallback — if `A` and `B` share no topology (including `_default`), the edge is denied.

`_default` exists precisely to keep the empty-state ergonomic: the moment no user topology contains an agent, that agent rejoins `_default` and can again talk freely with other unaffiliated peers.

## Mutation cascade

- `reyn agent rm <name>` removes `<name>` from every topology's `members`.
- `team` topologies whose leader is removed are deleted entirely.
- Topologies whose `members` becomes empty are deleted.

## See also

- [Concepts: topology](../../concepts/topology.md)
- [Reference: topology CLI](../cli/topology.md)
- [Reference: profile-yaml](profile-yaml.md)
