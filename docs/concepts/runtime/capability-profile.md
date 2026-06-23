---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_skills, mcp filter, tool restriction, category visibility, profile.yaml, self-edit, hot-reload, autonomous edit]
---

# Capability profile

A capability profile is a per-agent specification that declares two things:
the agent's **identity** (name, role) and its **capability restrictions** (which
skills, MCP servers, tools, and tool categories the agent may use).

It is stored at `.reyn/agents/<name>/profile.yaml` and loaded at session
construction. The `default` agent is created automatically on first `reyn chat`.

## One spec, two binding adapters

The capability profile is one piece of data with two distinct consumers:

```
profile.yaml
    │
    ├─→ AgentLayer   (authorization grant baseline: skill allowlist → router catalog)
    │
    └─→ ProfileLayer (conjunctive restrict: ∩ with AgentLayer + SandboxLayer)
```

**AgentLayer** uses the profile to filter the skill catalog before the router
LLM sees it. This is the "what can this agent do?" surface — the router's
available options.

**ProfileLayer** participates in the runtime conjunctive restrict model
(`effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer`). ProfileLayer is
restrict-only: it can narrow what AgentLayer grants, but can never re-grant
something AgentLayer denied. The conjunction is structural — no layer's `False`
can be overridden.

Both adapters read from the same profile spec. Adding a restriction to the
profile narrows both the catalog (AgentLayer) and the runtime gate (ProfileLayer)
simultaneously, without changing `EffectivePermission` logic.

For the full ∩-model, see [Permission model § conjunctive restrict model](permission-model.md#effective-permission-conjunctive-restrict-model).

## Capability axes

The unified profile spec carries four restriction axes. All are optional; an
absent or `null` axis means unrestricted on that dimension.

### `allowed_skills` (skill allowlist)

Controls which skills are offered to the router LLM.

| Value | Meaning |
|-------|---------|
| absent / `null` | **Unrestricted.** All project + stdlib skills are offered. |
| `[]` | **Router-only.** No skill spawn; router may reply directly or delegate. |
| `[a, b, c]` | **Allowlist.** Only the listed skill names are offered. |

System skills (`skill_router`, `chat_compactor`, `skill_narrator`) are always
enabled — they are not subject to this list.

Two-layer enforcement: the router narrows `available_skills` before the LLM
sees the catalog; `_spawn_skill` re-checks at launch time as defense-in-depth.

### `allowed_mcp` (MCP server allowlist) — ⏳ staged: #2074-S1

Restricts which MCP servers the agent may call, independent of which are
installed. Filters the per-agent intersection at MCP call time.

`null` = unrestricted (all configured servers available). A list restricts to
the named server IDs.

Note: `allowed_mcp` is an ACL filter, not a capability grant — it narrows an
already-granted `mcp` permission, it does not grant MCP access on its own. See
[Permission model § allowed_mcp](permission-model.md#axes).

### `tool_policy` (per-tool allow/deny) — ⏳ staged: #2074-S1

Per-named-tool allow or deny entries applied at dispatch time before the tool
reaches the LLM.

`null` = unrestricted. A list of `{tool: <name>, policy: allow|deny}` entries.
Deny entries take precedence over allow entries for the same tool name.

### `category_visibility` (tool category visibility) — ⏳ staged: #2074-S1

Controls which tool categories are visible to the agent. Categories group tools
by function (e.g., `file`, `shell`, `web`, `mcp`).

`null` = all categories visible. A list restricts visibility to the named
categories.

## Reload model

Profile changes take effect at the **next session startup**. The profile is
loaded once at session construction; a running session reads from the in-memory
copy.

**Turn-boundary hot-reload is planned** — edits to `profile.yaml` will be
picked up between turns without restarting the session. This is being designed
as part of the autonomous-edit workflow (⏳ #20, sequenced after #2074).

## Agent self-edit

An agent can edit its own capability profile at runtime without requesting extra
permissions:

**Path:** `.reyn/agents/<agent_name>/profile.yaml`

**Write permission:** The `.reyn/` tree is the default write zone. `.reyn/agents/`
is not a protected path (unlike `.reyn/approvals.yaml`), so a standard
`file.write` to this path requires **no extra declaration** — it is within the
default write grant.

**Verification:** `_DEFAULT_WRITE_ZONES = (".reyn",)` and
`_CANONICAL_PROTECTED_WRITE_PATHS` does not include `.reyn/agents/` (confirmed
in `src/reyn/security/permissions/permissions.py`).

**Procedure:** read the current `profile.yaml` → modify the relevant axis →
write back. Changes take effect at next startup (today) or next turn
(⏳ hot-reload, #20).

**Self-edit use case:** the canonical autonomous-edit goal is for the agent to
narrow its own capability profile during a session — e.g., write
`allowed_skills: [skill_a, skill_b]` to restrict itself to a focused task set.
Once hot-reload (#20) lands, this takes effect immediately at the next turn
boundary.

## Schema example

```yaml
name: researcher
role: |
  Deep technical research, prefers primary sources.
created_at: 2026-05-01T12:00:00+00:00
allowed_skills:
  - web_search
  - recall_docs
# axes below staged in #2074-S1:
allowed_mcp:          # null = unrestricted
  - github-mcp
tool_policy:          # null = unrestricted
  - tool: shell_exec
    policy: deny
category_visibility:  # null = all visible
  - file
  - web
```

Full schema reference: [profile.yaml reference](../../reference/dsl/profile-yaml.md).

## See also

- [Permission model](permission-model.md) — the ∩-model, authorization layers, axis taxonomy
- [Permission model § conjunctive restrict](permission-model.md#effective-permission-conjunctive-restrict-model) — ProfileLayer in the ∩
- [Reference: profile.yaml](../../reference/dsl/profile-yaml.md) — full schema + agent self-edit guide
- [Reference: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`, `reyn agent list`
- [Concepts: multi-agent](../multi-agent/multi-agent.md) — how agents are composed
