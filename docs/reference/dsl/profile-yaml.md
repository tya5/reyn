---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [profile.yaml]
search_hints: [profile.yaml, allowed_skills, allowed_mcp, tool_policy, category_visibility, agent self-edit, capability restriction, profile reload]
---

# `profile.yaml`

Per-agent metadata at `.reyn/agents/<name>/profile.yaml`. Created by `reyn agent new`; loaded on every `reyn chat` startup.

Concept background: [Capability profile](../../concepts/runtime/capability-profile.md).

## Schema

```yaml
name: researcher                       # required (== directory name)
role: |                                # optional, default ""
  Deep technical research, prefers
  primary sources (arxiv, RFCs).
created_at: 2026-05-01T12:00:00+00:00  # ISO-8601 UTC, set by `reyn agent new`
allowed_skills:                        # optional, default null (unrestricted)
  - web_search
  - recall_docs
# axes below staged in #2074-S1:
allowed_mcp: null                      # optional, default null (unrestricted)
tool_policy: null                      # optional, default null (unrestricted)
category_visibility: null              # optional, default null (all visible)
```

## Fields

### `name` (string, required)

Agent name. Must match `^[a-z0-9][a-z0-9_-]{0,31}$` and equal the parent directory name. The `default` agent is reserved and auto-created on first `reyn chat`.

### `role` (string, default `""`)

Free-form text injected into the agent's LLM system prompt as a `━━━ AGENT ROLE ━━━` block. Keep it short and behaviorally specific — this is what differentiates the agent from peers without changing skills.

Empty role is fine; the agent then behaves like a generalist with no extra persona.

### `created_at` (string, default `""`)

ISO-8601 UTC timestamp set when `reyn agent new` runs. Cosmetic; not consulted at runtime.

### `allowed_skills` (`list[str]` | `null`, default `null`)

Skill allowlist. Three states with distinct meaning:

| Value | Meaning |
|-------|---------|
| absent / `null` | **Unrestricted.** Every project + stdlib skill is offered to the router LLM. |
| `[]` (empty list) | **Router-only.** No skill spawn happens; the router can still reply directly or delegate to another agent. |
| `[a, b, c]` | **Allowlist.** Only the listed skill names are offered. |

Stdlib router (`skill_router`), compactor (`chat_compactor`), and narrator (`skill_narrator`) are **always** enabled and are not subject to this list — they're system skills, not agent-selectable.

Two-layer enforcement:

1. **Router-side filter** — `_invoke_router` narrows `available_skills` to the allowlist before the LLM sees the catalogue.
2. **Defense in depth** — `_spawn_skill` re-checks at launch time. A blocked spawn surfaces an `error` in the outbox and a `skill_spawn_refused` event with `reason="allowlist"`.

### `allowed_mcp` (`list[str]` | `null`, default `null`) — ⏳ staged: #2074-S1

MCP server allowlist. Restricts which MCP servers the agent may call, independent of which are installed.

`null` = unrestricted (all configured servers available). A list of server IDs restricts to those named servers only.

This is an ACL filter — it narrows an already-granted `mcp` permission; it does not grant MCP access on its own.

### `tool_policy` (`list[{tool, policy}]` | `null`, default `null`) — ⏳ staged: #2074-S1

Per-named-tool allow or deny entries. Applied at dispatch time, before the tool reaches the LLM.

`null` = unrestricted. Each entry is `{tool: <name>, policy: allow|deny}`. Deny entries take precedence over allow entries for the same tool name.

### `category_visibility` (`list[str]` | `null`, default `null`) — ⏳ staged: #2074-S1

Tool category visibility filter. Categories group tools by function (e.g. `file`, `shell`, `web`, `mcp`).

`null` = all categories visible. A list restricts visibility to the named categories only.

## Reload behavior

Profile changes take effect at the **next session startup**. The profile is loaded once at session construction; a running session uses its in-memory copy.

Turn-boundary hot-reload is planned — edits to `profile.yaml` will be picked up between turns without restarting (⏳ #20, sequenced after #2074).

## Agent self-edit

An agent can rewrite its own profile without requesting extra permissions:

**Write permission:** `.reyn/agents/` is within the default write zone (`.reyn/`). It is **not** a protected path (unlike `.reyn/approvals.yaml`), so no extra `file.write` declaration is needed.

**Procedure:**

1. Read `.reyn/agents/<agent_name>/profile.yaml` (Control IR `file.read`).
2. Modify the relevant axes.
3. Write back (Control IR `file.write` — within default zone, no extra declaration).
4. Changes take effect at next startup; with ⏳ #20 hot-reload, at next turn boundary.

The canonical autonomous-edit use case: an agent narrows its own `allowed_skills` mid-session to focus on a specific task set, then restores full access after.

## Editing (human operator)

`reyn agent new --role` writes a fresh profile. To change any field afterwards, edit the file directly — there is no `reyn agent set-skills` CLI yet (residual). The format is permissive about ordering and trailing keys.

## See also

- [Concepts: Capability profile](../../concepts/runtime/capability-profile.md) — one-spec/two-binding framing, reload model, agent self-edit details
- [Reference: agent CLI](../cli/agent.md)
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [Reference: skill_router](../stdlib/skill_router.md) — how `available_skills` reaches the LLM
- [Concepts: Permission model](../../concepts/runtime/permission-model.md) — ProfileLayer in the ∩ model
