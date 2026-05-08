---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — Chat mode

`reyn chat` is an interactive REPL attached to an *agent* — the lowest-friction way to see Reyn in action before you author your own skill. Each turn runs through `skill_router`, which classifies the intent and either replies, runs a stdlib skill, or delegates to another agent. Memory is recalled and written automatically.

## Start a session

```bash
reyn chat
```

That attaches to the auto-created `default` agent. To attach to a specific named agent:

```bash
reyn chat researcher
```

Type a turn:

```
> summarize the README of this project
```

The router picks `text_summarizer` (or whatever stdlib/project skill best matches), runs it, and prints the result. Each turn stays in the same session, persisted under `.reyn/agents/<name>/`.

## Slash commands

Lines starting with `/` are intercepted as control commands, not routed:

- `/list` — running skill spawns and pending interventions
- `/cancel <id>` — cancel a skill spawn
- `/answer <id> <text>` — answer a pending `ask_user` / permission prompt
- `/agents` — list loaded agents in this process
- `/attach <name>` — switch the REPL to another agent

## Multiple agents

You can spin up named agents with their own roles and skill allowlists:

```bash
reyn agent new researcher --role "deep technical research, prefers primary sources"
reyn agent new writer     --role "concise long-form prose"
```

In a chat session attached to `default`, the router may decide a request is better handled by `researcher` and emit a delegation. The reply auto-routes back; you'll see an interim acknowledgement followed by a synthesized final answer. Use `/attach researcher` to watch progress mid-chain.

For structural restrictions on who-can-talk-to-whom, see [topology CLI](../../reference/cli/topology.md) and [concepts/topology](../../concepts/topology.md).

## How the router picks

`skill_router` reads `user_message`, the available skills (filtered by `profile.allowed_skills` if set), reachable peer agents (filtered by topology rules), and the merged memory index. It picks one path: skill, agent, or direct reply. If you want to force a particular skill, ask explicitly ("use skill_builder to ...") — the router uses the cue.

## Memory is automatic

The router phase reads two memory layers on every turn (no extra config needed):

- **Shared** — `.reyn/memory/` — facts visible to every agent
- **Agent** — `.reyn/agents/<name>/memory/` — facts scoped to this agent

Writes happen inside the same router turn that detected something durable. See [concepts/memory](../../concepts/memory.md) for the full model.

## Inspecting and managing memory

The `reyn memory` CLI operates on the **shared** layer by default:

```bash
reyn memory list             # show all stored memories
reyn memory show <slug>      # print one
reyn memory edit <slug>      # open in $EDITOR
reyn memory delete <slug>    # remove
```

Pass `--agent <name>` to operate on an agent-scoped layer instead:

```bash
reyn memory list --agent researcher
reyn memory delete --agent researcher feedback_arxiv
```

Mutating commands (`edit`, `delete`, `import`) automatically rebuild the layer's `MEMORY.md` after the change — the index never drifts from the on-disk body files.

## Why chat mode is just a router skill

The OS doesn't know about "chat" — it just runs a skill. `skill_router` is a normal stdlib skill that happens to choose another skill (or peer agent) to delegate to. This is the same composition pattern as any other reyn skill (P7).

## What you learned

- `reyn chat [agent_name]` attaches a REPL to an agent.
- Slash commands manage spawns, interventions, and agent switching.
- The router can delegate to peer agents; chains synthesize back to the user.
- Memory is two-layered (shared + agent), read and written automatically.

## Where to go next

You've seen Reyn working end-to-end as a chat agent. From here:

- **[Tutorial 03 — Your first skill](03-your-first-skill.md)** — author a skill from a one-line description with `skill_builder`.
- **[Tutorial 04 — Running a skill](04-running-a-skill.md)** — run a skill from the CLI in depth (input formats, flags, event log).
- **[Tutorial 05 — Writing an eval](05-writing-an-eval.md)** — pin behaviour with a rubric.
- **Multi-agent**: read [concepts/multi-agent](../../concepts/multi-agent.md) and [concepts/topology](../../concepts/topology.md) to compose specialist agents into a team.
- **Read the [principles](../../concepts/principles.md).** Understanding the eight principles makes everything in the reference make sense.
