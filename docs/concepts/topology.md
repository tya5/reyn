---
type: concept
topic: architecture
audience: [human, agent]
---

# Topology

Topology declares **who-can-talk-to-whom** as structure. Three first-class kinds exist (`network`, `team`, `pipeline`); an auto-managed `_default` topology makes the empty state ergonomic. The result is a single permit rule that fits in a few lines of code.

## Why topology is first-class

Before topology, agents in a process formed an implicit complete graph — anyone could send to anyone, and the only safety rail was `max_hop_depth`. That works for two-agent toy setups but breaks down for organizational structure: a three-team org expressed as ad-hoc filters quickly becomes inconsistent.

reyn's stance: model the structure once, enforce it everywhere. AutoGen / CrewAI / LangGraph each pick *one* hardcoded shape (GroupChat manager, hierarchical, supervisor); reyn makes the shape declarative.

## Three kinds

Each kind is a YAML file at `.reyn/topologies/<name>.yaml` with `name`, `kind`, `members`, and (for `team`) `leader`. The `can_send(A, B)` rule per kind:

| Kind | Rule |
|------|------|
| `network` | `A != B and A,B ∈ members` — complete graph |
| `team` | `leader ∈ {A, B} and A != B and A,B ∈ members` — star around leader; peer ↔ peer is forbidden |
| `pipeline` | `members.index(B) == members.index(A) + 1` — directed path, no jumps, no reverse |

Examples:

```yaml
# network: a free-flowing team of peers
name: kitchen
kind: network
members: [chef, sous, baker]
```

```yaml
# team: manager + workers, workers cannot bypass manager
name: research_lead
kind: team
leader: manager
members: [manager, researcher_a, researcher_b]
```

```yaml
# pipeline: triage → drafting → publishing
name: publish_pipe
kind: pipeline
members: [triage, drafter, publisher]
```

## `_default` topology

The registry auto-synthesizes a `_default` network topology containing every agent that is **not** a member of any user-declared topology. It's in-memory, not persisted.

This makes the empty state ergonomic — declare zero topologies and `_default` covers everyone, so the runtime is fully permissive. The moment you add an agent to a user-declared topology, it leaves `_default` and only the user-declared rule applies. Restriction is enforced the instant it's declared.

`_default` shows up in `reyn topology list` for transparency:

```
NAME      KIND      MEMBERS
team1     team      default*, alpha
_default  network   beta, gamma
```

You cannot create, remove, or mutate `_default` directly — it's automatic.

## Single permit rule

```python
def permit(from_agent, to_agent):
    if from_agent == to_agent:
        return False
    candidates = list(user_topologies) + [default_topology()]
    shared = [t for t in candidates if from_agent in t.members and to_agent in t.members]
    if not shared:
        return False
    return any(t.can_send(from_agent, to_agent) for t in shared)
```

That's it. No fallback, no policy mode, no per-agent override. Multiple topologies that overlap each contribute their `can_send`; the edge is permitted if **any** of them allows it.

## Where the rule fires

Two enforcement points (defense in depth):

1. **`iter_reachable_agents`** — when the router builds its `available_agents` list, agents the caller cannot reach are filtered out. The LLM never sees an unreachable target, so it can't propose blocked delegations.
2. **`_send_to_agent`** — at send time, `permit()` is consulted. A blocked send surfaces an `error` outbox message ("agent X: blocked by topology rules") and an `agent_message_sent` is **not** emitted.

## Tree pattern

There's no `tree` kind. Hierarchies are expressed as **overlapping `team` topologies**:

```yaml
# .reyn/topologies/team_exec.yaml
name: team_exec
kind: team
leader: ceo
members: [ceo, vp_eng, vp_sales]
```

```yaml
# .reyn/topologies/team_eng.yaml
name: team_eng
kind: team
leader: vp_eng
members: [vp_eng, eng_a, eng_b]
```

```yaml
# .reyn/topologies/team_sales.yaml
name: team_sales
kind: team
leader: vp_sales
members: [vp_sales, sales_a]
```

Result:

| Edge | Permitted? | Why |
|------|------------|-----|
| `ceo ↔ vp_eng` | ✓ | `team_exec` (leader ↔ member) |
| `vp_eng ↔ eng_a` | ✓ | `team_eng` (leader ↔ member) |
| `vp_eng ↔ vp_sales` | ✗ | `team_exec` (peer ↔ peer forbidden) |
| `ceo ↔ eng_a` | ✗ | no shared topology — must escalate via vp_eng |
| `eng_a ↔ eng_b` | ✗ | `team_eng` (peer ↔ peer forbidden) |

Only direct parent ↔ child edges are permitted, exactly as a tree should. Multi-level escalation happens through repeated single hops (`ceo → vp_eng`, then `vp_eng → eng_a`), which integrates naturally with [chain_id](multi-agent.md#chain_id) for end-to-end tracing.

A `validate-tree` command (residual) would check that a set of overlapping team topologies actually forms a tree (single root, no cycles, no multi-parent) — useful for rigor but not required for the runtime to work.

## Agent removal cascade

`reyn agent rm <name>` cascades into topologies:

- The agent is dropped from every topology's `members`.
- If a `team`'s leader is removed, the entire topology is deleted (a leaderless team is meaningless).
- Topologies whose `members` becomes empty are also deleted.

After cascade, the registry recomputes `_default`; agents who lost their last user-declared affiliation rejoin `_default` automatically.

## See also

- [Reference: topology CLI](../reference/cli/topology.md)
- [Reference: topology-yaml](../reference/dsl/topology-yaml.md)
- [Concepts: multi-agent](multi-agent.md)
