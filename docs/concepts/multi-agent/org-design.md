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
| `spawn_agent` | Create a child agent with a name + role, capped at ⊆ your capabilities |
| `spawn_session` | Start a fresh-context sub-session to run a task in isolation |
| `create_topology` | Wire agents you spawned into a communication topology and optionally narrow each member's capabilities |

These tools are **router-only** (not available inside a Phase): they are
org-design decisions made by the running agent, not instructions authored in a
workflow.

> **Distinct from the operator topology tools.** The [operator CLI
> (`reyn topology`)](../../reference/cli/topology.md) and
> Topology YAML let a *human operator*
> define the org structure up front in configuration. The tools on this page let
> the *LLM itself* design the org at runtime — they are complementary, not
> competing, surfaces. An operator-authored topology remains the authority for
> any agent that is already a member; the LLM can only build within its own
> spawn subtree.

---

## `spawn_agent` — create a child agent

```text
spawn_agent(name: str, role: str = "")
```

Creates a new agent in the registry under your authority. The new agent's
spawn lineage is set by the OS, not by the LLM (forge-guard: the LLM
never supplies the parent link). The new agent's effective capability is
**capped at a subset of yours by construction** — it can never do anything
you cannot (see [⊆-parent capability model](../runtime/permission-model.md#llm-spawn-capability-model)).

Use `spawn_agent` to design the *identity* layer of your org: who exists
and what their role is. To control *who-can-talk-to-whom* and narrow
capabilities further, use `create_topology`.

### What the return value tells you

`spawn_agent` returns a spawn-ack (synchronous) — the agent is created and
registered before the tool returns. The ack includes the new agent's name
so you can reference it in a subsequent `create_topology` call.

---

## `spawn_session` — run a task in a fresh context

```text
spawn_session(request: str, mode: "ephemeral" | "persistent" = "persistent",
              narrowing: dict | None = None, base_dir: str | None = None,
              agent: str | None = None, session: str | None = None)
```

Starts a new Session under your current agent (or, with `agent`, #4556:
under any agent in your own spawn subtree) to run `request` in isolation —
a blank context window, independent workspace, with no memory of this
conversation. The spawned session begins immediately; this tool returns a
spawn-ack rather than waiting for the task to complete (async dispatch).

**`agent`** (optional, #4556): target any agent in your own spawn subtree —
yourself, or an agent you created (transitively) via `spawn_agent`. Guarded
by the same `is_spawn_descendant` predicate `create_topology` already uses
for its own subtree forge-guard — an attempt to target an agent outside your
subtree is rejected (`agent_outside_subtree`), not silently redirected.
Omit to spawn under your own agent (the default, unchanged behavior).

**`session`** (optional, #4556): choose the new session's id yourself
instead of letting the OS auto-generate one. A duplicate id for the target
agent is rejected (`session_already_exists`), never silently overwritten.

**Reachable under exclusive-wrapper mode too (#3896, fixed 2026-08-13).** When
[`action_retrieval.universal_wrappers_enabled`](../tools-integrations/universal-catalog.md)
is `true`, `spawn_session`'s individual per-tool entry is stripped from direct
advertisement, but the `multi_agent` catalog category now includes it
alongside `list_agents` / `describe_agent` — `invoke_action{action_name:
"spawn_session", args: {...}}` reaches the same handler, same permission
enforcement, as calling it directly. (An earlier version of this doc — and of
the code itself — documented this as a real gap: #3896 found that
`spawn_session` used to have no compensating catalog route at all, a genuine
capability loss under a real, selectable configuration. Owner ruling gave it
one rather than accept the loss.) `run_prompt` / `send_to_session` remain
router-only delegation tools with no catalog-dispatch fallback — this fix is
scoped to `spawn_session`'s own finding, not the whole delegation surface.
(Prior to proposal 0067 P6, #3978, `delegate_to_agent` was the one *other*
exception with a catalog-channel route; it retired with the rest of its own
tool.)

**`mode`**:

- `ephemeral` — the session auto-vanishes after its task completes. Use
  for one-shot work where you want no lingering state. "Completes" means
  no pending work, not merely an empty inbox: if the ephemeral session
  has itself delegated to a peer and is awaiting that peer's response, a
  transiently-empty inbox mid-wait does not trigger teardown — vanishing
  before the response lands would purge the session the reply is
  addressed to. A spawned session may itself call `spawn_session` again
  (nesting is supported); its result still routes back correctly.
- `persistent` — the session stays registered after the task. Use when
  you need to refer back to it or continue work there.

**`narrowing`** (optional): a capability-profile subset imposed on the
sub-session at construction time. Restrict-only — you cannot grant the
sub-session capabilities beyond your own. Example:

```json
{"tool_deny": ["exec"]}
```

Restrict-only is enforced, not assumed: whatever you pass is **composed**
with the spawning session's own per-session narrowing before the child is
constructed — denies union, allow-lists intersect, and an axis you say
nothing about keeps the spawner's restriction on it. So omitting
`narrowing` entirely gives the sub-session the spawner's envelope, never a
wider one. (The composition covers the sid-keyed per-session layer; the
agent-level layers need no carrying, since the sub-session runs under the
same agent identity.)

**`base_dir`** (optional, #4200): a working-directory override for the
spawned session. Restrict-only, same shape as `narrowing` — must resolve
inside YOUR OWN effective `base_dir`; a path outside it is REJECTED, never
silently clamped into it. Relative paths resolve against your own
`base_dir`. Omit to inherit your own `base_dir` unchanged (the default).

Both modes are rewind-safe: a session spawned after a rewind cut is
dropped during rewind reconstruction.

---

## `create_topology` — wire and narrow your spawn subtree

```text
create_topology(
    name: str,
    kind: "network" | "team" | "pipeline",
    members: list[str],
    leader: str | None = None,      # required for kind=team
    profiles: dict[str, str] | None = None,
)
```

Creates a named communication topology from agents **in your spawn
subtree** (yourself plus any agent you created via `spawn_agent`,
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
spawn_agent(name="researcher", role="gather background on topic X")
spawn_agent(name="writer",     role="draft the section from findings")

# 2. Wire them and optionally narrow
create_topology(
    name="research_team",
    kind="team",
    leader="researcher",   # researcher coordinates writer
    members=["researcher", "writer"],
    profiles={"writer": "no_subprocess"},
)

# 3. Spawn an isolated task for a one-off need
spawn_session(
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
| `safety.spawn.max_children` | `20` | Maximum direct spawn-children per parent, and maximum member count in a `create_topology` call |

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

`max_children` counts a parent by **identity, not name**: if a parent is
purged and its name reused, a pre-purge orphan child does not count against
the new, same-named parent's budget — the reused name gets its full
`max_children` allowance, not a reduced one. This is the fan-out-accounting
counterpart to the identity-keyed lineage rule in
[permission-model.md § No-escalation-via-spawn](../runtime/permission-model.md#no-escalation-via-spawn-the-closed-class):
that rule is about capability re-grant after a name reuse, this one is
about the DoS-bound count.

See [reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) and
[safety.on_limit](../../reference/config/reyn-yaml.md#safetyon_limit-fields) for full schema.

---

## See also

- [⊆-parent capability model](../runtime/permission-model.md#llm-spawn-capability-model) — how the no-escalation-via-spawn security property is enforced
- [Concepts: topology (operator)](../multi-agent/topology.md) — the human-CLI org-design surface
- [Concepts: sessions](../multi-agent/sessions.md) — what a session owns; ephemeral / persistent lifecycle
- [Reference: reyn-yaml § safety.spawn](../../reference/config/reyn-yaml.md#safetyspawn-fields) — operator bounds

