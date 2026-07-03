---
type: concept
topic: multi-agent
audience: [human, agent]
---

# LLM org-design tools

Reyn gives an LLM three primitives for building a live multi-agent
organisation at runtime:

| Tool | What it does |
|------|-------------|
| `agent_spawn` | Create a child agent with a name + role, capped at ⊆ your capabilities |
| `session_spawn` | Start a fresh-context sub-session to run a task in isolation |
| `topology_create` | Wire agents you spawned into a communication topology and optionally narrow each member's capabilities |

These tools are **router-only** (not available inside a Phase): they are
org-design decisions made by the running agent, not instructions authored in a
skill.

> **Distinct from the operator topology tools.** The [operator CLI
> (`reyn topology`)](../../reference/cli/topology.md) and
> Topology YAML let a *human operator*
> define the org structure up front in configuration. The tools on this page let
> the *LLM itself* design the org at runtime — they are complementary, not
> competing, surfaces. An operator-authored topology remains the authority for
> any agent that is already a member; the LLM can only build within its own
> spawn subtree.

---

## `agent_spawn` — create a child agent

```text
agent_spawn(name: str, role: str = "")
```

Creates a new agent in the registry under your authority. The new agent's
spawn lineage is set by the OS, not by the LLM (forge-guard: the LLM
never supplies the parent link). The new agent's effective capability is
**capped at a subset of yours by construction** — it can never do anything
you cannot (see [⊆-parent capability model](../runtime/permission-model.md#llm-spawn-capability-model)).

Use `agent_spawn` to design the *identity* layer of your org: who exists
and what their role is. To control *who-can-talk-to-whom* and narrow
capabilities further, use `topology_create`.

### What the return value tells you

`agent_spawn` returns a spawn-ack (synchronous) — the agent is created and
registered before the tool returns. The ack includes the new agent's name
so you can reference it in a subsequent `topology_create` call.

---

## `session_spawn` — run a task in a fresh context

```text
session_spawn(request: str, mode: "ephemeral" | "persistent" = "persistent",
              narrowing: dict | None = None)
```

Starts a new Session under your current agent to run `request` in
isolation — a blank context window, independent workspace, with no memory
of this conversation. The spawned session begins immediately; this tool
returns a spawn-ack rather than waiting for the task to complete (async
dispatch).

**`mode`**:

- `ephemeral` — the session auto-vanishes after its task completes. Use
  for one-shot work where you want no lingering state.
- `persistent` — the session stays registered after the task. Use when
  you need to refer back to it or continue work there.

**`narrowing`** (optional): a capability-profile subset imposed on the
sub-session at construction time. Restrict-only — you cannot grant the
sub-session capabilities beyond your own. Example:

```json
{"tool_deny": ["sandboxed_exec"]}
```

Both modes are rewind-safe: a session spawned after a rewind cut is
dropped during rewind reconstruction.

---

## `topology_create` — wire and narrow your spawn subtree

```text
topology_create(
    name: str,
    kind: "network" | "team" | "pipeline",
    members: list[str],
    leader: str | None = None,      # required for kind=team
    profiles: dict[str, str] | None = None,
)
```

Creates a named communication topology from agents **in your spawn
subtree** (yourself plus any agent you created via `agent_spawn`,
transitively). The `can_send(A, B)` rule follows the same three kinds as
operator-authored topologies:

| Kind | Who can send to whom |
|------|----------------------|
| `network` | Every member ↔ every member |
| `team` | Only through the leader — peer ↔ peer is forbidden |
| `pipeline` | Each member → next member only |

### `profiles` — narrow member capabilities

`profiles` maps an agent name to a `capability_profile` name. A bound
member's session is restricted by that profile on top of the existing
⊆-parent cap — it can only narrow *within* the envelope it already has,
never widen it. Profiles are loaded from `.reyn/capability_profiles/<name>.yaml`.

```json
{
  "worker_a": "read_only",
  "worker_b": "no_subprocess"
}
```

### Spawn-subtree restriction (forge-guard)

You may only include agents in your own spawn subtree as members. The OS
enforces this at the topology-create seam — an attempt to wire an agent
you did not create (or that is not a transitive spawn-child of yours) is
rejected. This keeps profile bindings safe by construction: every bound
member is already ⊆ you via the lineage conjunct, so a binding can only
narrow within that envelope.

The topology is WAL-tracked so it survives crash recovery and rewind.

---

## Putting it together: a typical org-design flow

```text
# 1. Create team members
agent_spawn(name="researcher", role="gather background on topic X")
agent_spawn(name="writer",     role="draft the section from findings")

# 2. Wire them and optionally narrow
topology_create(
    name="research_team",
    kind="team",
    leader="researcher",   # researcher coordinates writer
    members=["researcher", "writer"],
    profiles={"writer": "no_subprocess"},
)

# 3. Spawn an isolated task for a one-off need
session_spawn(
    request="translate the draft to Japanese",
    mode="ephemeral",
)
```

---

## Operator-set bounds on the LLM spawn tree

An operator can bound how large an LLM-designed org can grow using
`safety.spawn` in `reyn.yaml`. These are DoS guards — they prevent an
agent from minting an unbounded organisation. The LLM has no runtime path
to raise its own base limit (the config is the restart-only OUT layer).

| Key | Default | Effect |
|-----|---------|--------|
| `safety.spawn.max_depth` | `10` | Maximum spawn-lineage chain depth (0 = unlimited) |
| `safety.spawn.max_children` | `20` | Maximum direct spawn-children per parent, and maximum member count in a `topology_create` call |

When a spawn would exceed a limit, the `safety.on_limit` checkpoint fires — the same
mode-driven framework used by loop and budget caps:

- **`interactive`** (default): the operator is prompted to approve an extension. On
  approval, the extension is recorded per-spawner so the same scope does not re-prompt.
  The base config limit stays unchanged — any extension is operator-approved, never
  LLM-driven.
- **`unattended`**: the spawn is rejected immediately (no prompt possible — use for CI
  or scripted runs).
- **`auto_extend`**: extensions are auto-approved up to `auto_extend_times` times, then
  rejected.

`max_depth` and `max_children` carry separate per-spawner extension keys: an
operator-approved increase in one does not silently widen the other.

See [reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) and
[safety.on_limit](../../reference/config/reyn-yaml.md#safetyonlimit-fields) for full schema.

---

## See also

- [⊆-parent capability model](../runtime/permission-model.md#llm-spawn-capability-model) — how the no-escalation-via-spawn security property is enforced
- [Concepts: topology (operator)](../multi-agent/topology.md) — the human-CLI org-design surface
- [Concepts: sessions](../multi-agent/sessions.md) — what a session owns; ephemeral / persistent lifecycle
- [Reference: reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) — operator bounds

