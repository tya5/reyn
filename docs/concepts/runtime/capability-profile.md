---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_skills, allowed_mcp, tool_allow, tool_deny, categories, category visibility, self-edit, named capability, untrusted narrowing]
---

# Capability profile

The capability profile system has two distinct surfaces that serve complementary
roles: the per-agent identity file (`profile.yaml`) and the named capability
spec (`capability_profiles/<name>.yaml`).

## Two surfaces

### `profile.yaml` — per-agent identity (`AgentProfile`)

Stored at `.reyn/agents/<name>/profile.yaml`. Loaded at session construction.
Carries the agent's identity and coarse-grained allowlists:

- `name`, `role`, `created_at` — identity
- `allowed_skills` — skill allowlist (which skills the router offers this agent)
- `allowed_mcp` — MCP server allowlist (which servers this agent may call)

These two allowlists participate in the runtime ∩-gate as the **ProfileLayer**:
`effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer`.

Full schema: [profile.yaml reference](../../reference/dsl/profile-yaml.md).

### `capability_profiles/<name>.yaml` — named capability spec (`CapabilityProfile`)

Stored at `.reyn/capability_profiles/<name>.yaml`. A named, declarative
narrowing of tool-level capabilities. One project can define many profiles; a
running agent may have one or more applied simultaneously.

This is the surface introduced by #1827 and extended through its staging arc.

## `CapabilityProfile` axes

A capability profile carries two independent narrowing axes:

### Axis A — enforcement (`tool_allow` / `tool_deny`)

Tool-level allow/deny control. Produces a `ContextualPermission` that rides
the live ∩-gate alongside the existing permission layers.

| Field | Type | Semantics |
|-------|------|-----------|
| `tool_allow` | `list[str] \| null` | Allow-list. `null` = unconstrained (deny-list only). |
| `tool_deny` | `list[str]` | Deny-list. Union of denials across composed profiles. |

Deny entries always win over allow entries on the same tool name.

### Axis B — visibility (`categories`)

Cognitive narrowing: which tool categories remain visible to the agent. Derived
from `categories` against the canonical 12-entry catalog (`CATEGORIES`).

| Field | Type | Semantics |
|-------|------|-----------|
| `categories` | `list[str] \| null` | Categories to **keep visible**. `null` = no narrowing (all visible). `[]` = hide all. |

An unknown category name is a no-op (forward-compat — not an error).

Note: `visible ⊆ authorized` holds structurally — the visibility axis only
hides tools, it cannot re-grant tools that the enforcement axis denied.

## Composition model

When multiple profiles are applied simultaneously, `compose_resolved` merges
them under **most-restrictive-wins**:

- `tool_deny` → **union** (any profile's deny wins)
- `tool_allow` → **intersection** of all constraining allow-sets (`null` = ⊤,
  skipped); a tool stays allowed only if every constraining profile permits it
- `excluded_categories` → **union** (any profile's hide wins)

An empty profile list → inert result (byte-identical to no profile applied).

## Context-auto untrusted narrowing (S4)

One profile is auto-applied automatically — without any explicit binding —
while untrusted external content is live in the agent's context:

**Profile name:** `_untrusted` (built-in secure default, overridable via
`.reyn/capability_profiles/_untrusted.yaml`)

**Trigger:** any history/context entry whose meta carries `external_source=true`
(stamped by the content-fence seam at ingest time).

**Built-in default deny-set:** memory writes / deletes, re-delegation,
sandboxed execution, MCP install. The goal: untrusted content can be read and
reasoned about, but cannot drive irreversible actions.

This is seam-agnostic — the trigger is the meta marker, not the specific source.

## Binding modes

A `CapabilityProfile` is applied to a running session in one of two binding
modes:

- **Per-agent default** — one profile assigned as the default for an agent.
- **Per-context composable** — profiles composed dynamically from the live
  context (e.g., untrusted-source narrowing, ephemeral task scope).

The exact inline-vs-ref mechanism for how profiles bind to agents, topology, and
ephemeral scopes is being finalised (⏳ #2074-S4). Until S4 lands, only the
context-auto untrusted binding is wired end-to-end.

## Agent self-edit

An agent can create or update a capability profile without requesting extra
permissions:

**Path:** `.reyn/capability_profiles/<name>.yaml`

**Write permission:** `.reyn/capability_profiles/` is within the default write
zone (`.reyn/`). It is **not** a protected path (unlike `.reyn/approvals.yaml`),
so a standard `file.write` requires **no extra declaration**.

**Verification:** `_DEFAULT_WRITE_ZONES = (".reyn",)` and
`_CANONICAL_PROTECTED_WRITE_PATHS` contains only `.reyn/approvals.yaml` and
`.reyn/index/sources.yaml`. Confirmed in
`src/reyn/security/permissions/permissions.py`.

**Procedure:** write a YAML file with the desired `categories` / `tool_allow` /
`tool_deny` axes. The profile name (file stem) is how it is referenced for
binding (⏳ S4 wiring).

**Example:**

```yaml
name: read-only-researcher
description: "Deny all write/execute surfaces; allow read categories only."
categories:
  - file
  - web
tool_deny:
  - exec__sandboxed_exec
  - memory_operation__remember_shared
```

## Schema example

```yaml
# .reyn/capability_profiles/read-only-researcher.yaml
name: read-only-researcher        # required (== file stem)
description: ""                   # optional, default ""
categories:                       # optional; null = all visible
  - file
  - web
tool_allow: null                  # optional; null = unconstrained (deny-list only)
tool_deny:                        # optional, default []
  - exec__sandboxed_exec
  - multi_agent__delegate
```

Full field reference: a dedicated `capability-profiles-yaml.md` reference doc is planned (⏳ #2074-S4, inline-vs-ref surface).

## Relationship to the ∩-model

The `CapabilityProfile` enforcement axis produces a `ContextualPermission` that
participates in the conjunctive restrict model as a runtime ∩ term — it is
restrict-only and can never elevate a capability already denied by another layer.

The `AgentProfile.allowed_skills` / `allowed_mcp` fields participate as the
`ProfileLayer` in the same ∩-model.

For the full ∩-model, see [Permission model § conjunctive restrict model](permission-model.md#effective-permission-conjunctive-restrict-model).

## See also

- [Permission model](permission-model.md) — ∩-model, authorization layers
- [Permission model § conjunctive restrict](permission-model.md#effective-permission-conjunctive-restrict-model) — ProfileLayer in the ∩
- [Reference: profile.yaml](../../reference/dsl/profile-yaml.md) — AgentProfile schema (allowed_skills, allowed_mcp)
- [Concepts: multi-agent](../multi-agent/multi-agent.md) — agent composition
- [Reference: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`, `reyn agent list`
