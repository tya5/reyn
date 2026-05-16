---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [reyn agent, reyn topology]
---

# Build an agent team

**Goal:** Stand up a small team of specialist agents and constrain who-can-talk-to-whom so the structure matches your workflow.

## When to use

- You have one chat agent that's becoming a generalist with too many roles.
- You want to split work across specialists (e.g., research vs. drafting vs. review) and have them coordinate.
- You want to express "workers cannot bypass the lead" or a multi-level hierarchy.

## Quick recipe — leader + two workers

Three commands stand up a `team` topology with one leader and two workers.

### 1. Create the agents

```bash
reyn agent new lead --role "team lead. Triages requests and synthesizes worker output."
reyn agent new researcher --role "deep technical research, prefers primary sources (arxiv, RFCs)."
reyn agent new writer --role "concise long-form prose. Strict word budgets, no headings unless asked."
```

Each command provisions `.reyn/agents/<name>/profile.yaml` and seeds an empty memory layer.

### 2. Declare the team topology

```bash
reyn topology new launch --kind team \
    --leader lead \
    --members lead,researcher,writer
```

`team` kind permits `leader ↔ member` edges only. Workers cannot directly send to each other — they must route through `lead`.

### 3. Inspect the structure

```bash
reyn topology show launch
```

```
name:        launch
kind:        team
leader:      lead
members:     lead*, researcher, writer
created_at:  2026-05-01T12:00:00+00:00

permitted edges (4):
  lead → researcher
  lead → writer
  researcher → lead
  writer → lead
```

The asterisk marks the leader. Notice `researcher → writer` is **not** in the edge list — that's the team rule working.

### 4. Use it

```bash
reyn chat lead
```

Ask the lead a question that touches both research and drafting:

```
> Investigate DuckDB v1's breaking changes and produce a 200-word changelog summary.
```

The router on `lead` may emit `messages_to_agents` for `researcher` and (separately, after the response arrives) `writer`. From the user's seat, you'll see an interim "(working on it)" then a synthesized final reply. See [how-to: multi-hop delegation](multi-hop-delegation.md) for what's happening under the hood.

## Adding a member later

```bash
reyn agent new reviewer --role "edits drafts for clarity, never adds new claims."
reyn topology add-member launch reviewer
```

`reviewer` now has the same constraint: it talks only to `lead`.

## Removing a member

```bash
reyn agent rm researcher --yes
```

This cascades through every topology that listed `researcher` as a member — `launch` will end up with members `[lead*, writer, reviewer]`. Topologies that lose their leader, or end up empty, are removed entirely.

You can also drop a member without deleting the agent:

```bash
reyn topology rm-member launch writer
```

After this, `writer` no longer has a shared topology with `lead` (assuming `launch` was its only one), so `writer` rejoins the auto-managed `_default` topology and once again can talk freely with any other unaffiliated agent.

## Going to a 2-level tree

A real org isn't a single team — it's nested. There's no `tree` kind, but **overlapping team topologies** express a tree exactly:

```bash
# Three executives reporting to ceo
reyn agent new ceo --role "..."
reyn agent new vp_eng --role "..."
reyn agent new vp_sales --role "..."

# Engineers under vp_eng
reyn agent new eng_a --role "..."
reyn agent new eng_b --role "..."

# Sales under vp_sales
reyn agent new sales_a --role "..."

# Three teams, one per parent-team relationship
reyn topology new team_exec --kind team --leader ceo \
    --members ceo,vp_eng,vp_sales

reyn topology new team_eng --kind team --leader vp_eng \
    --members vp_eng,eng_a,eng_b

reyn topology new team_sales --kind team --leader vp_sales \
    --members vp_sales,sales_a
```

What you get:

| Edge | Permitted? | Why |
|------|------------|-----|
| `ceo ↔ vp_eng` | ✓ | `team_exec` (leader ↔ member) |
| `vp_eng ↔ eng_a` | ✓ | `team_eng` (leader ↔ member) |
| `vp_eng ↔ vp_sales` | ✗ | `team_exec` says peer ↔ peer is forbidden |
| `ceo ↔ eng_a` | ✗ | no shared topology — `ceo` must escalate via `vp_eng` |
| `eng_a ↔ eng_b` | ✗ | `team_eng` peer ↔ peer forbidden |

Multi-level escalation happens via repeated single hops (`ceo → vp_eng → eng_a`), bounded by `safety.loop.max_agent_hops` (default 3, raise it for deeper trees). See [concepts/topology — Tree pattern](../../concepts/topology.md#tree-pattern) for why this falls out of the design rather than needing a special kind.

## Picking a kind

| Kind | Use when |
|------|----------|
| `network` | Free-flowing peer team. No structural restriction; everyone can ask everyone. |
| `team` | A leader is the aggregation point. Workers shouldn't bypass them. |
| `pipeline` | Linear workflow (triage → draft → publish). Each stage talks only to the next. |

The auto-managed `_default` topology covers any agent you haven't placed in a user-declared topology — those agents stay freely reachable, which is what you want during early prototyping.

## Troubleshooting

**Router LLM never proposes delegation.** Check that the would-be target appears in `available_agents` for the source agent:

```bash
reyn topology show launch  # confirm both agents are members
```

If they share no topology, the edge is denied (the router can't even see the target), and a `_default` membership won't help once the source has been pulled into a user topology.

**`agent X: blocked by topology rules` in the outbox.** The LLM hallucinated a delegation target it shouldn't have proposed. Verify your topology kind matches your intent — for example, you might've declared `team` when you meant `network`.

**`agent message depth N exceeds limit M; chain refused`.** Your overlapping teams form a deeper tree than `safety.loop.max_agent_hops` allows. Raise the limit in `reyn.yaml`:

```yaml
safety:
  loop:
    max_agent_hops: 5
```

## See also

- [Concepts: topology](../../concepts/topology.md) — kind semantics, single permit rule, tree pattern
- [Concepts: multi-agent](../../concepts/multi-agent.md) — agent identity, AgentRegistry, chain semantics
- [Reference: topology CLI](../../reference/cli/topology.md)
- [Reference: agent CLI](../../reference/cli/agent.md)
- [How-to: multi-hop delegation](multi-hop-delegation.md) — what to expect when chains span multiple agents
