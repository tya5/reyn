---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_skills, allowed_mcp, tool_allow, tool_deny, skill_allow, skill_deny, mcp_allow, mcp_deny, categories, category visibility, ContextualLayer, ProfileLayer, self-edit, untrusted narrowing]
---

# Capability profile

The capability profile system is the unified narrowing primitive across all
`skill` / `mcp` / `tool` / `category` capability axes. It separates the
**spec** (what is narrowed) from the **binding** (when and how it applies).

Two binding adapters read one primitive. Both feed the same conjunctive ∩:

```
effective = AgentLayer ∩ SandboxLayer ∩ ProfileLayer ∩ ContextualLayer
```

For the full two-adapter design, see
[Permission model § One spec, two binding adapters](permission-model.md#effective-permission-conjunctive-restrict-model).

## Two surfaces, two operator files

### `AgentProfile` — `.reyn/agents/<name>/profile.yaml`

The per-agent identity and baseline allowlists. The operator writes this file
using the natural key names:

- `name`, `role`, `created_at` — identity
- `allowed_skills` — skill allowlist (maps internally to `skill_allow`)
- `allowed_mcp` — MCP server allowlist (maps internally to `mcp_allow`)

`AgentProfile.default_profile()` converts these keys to a `CapabilityProfile`
at runtime — no user-facing rename, same semantics. This feeds **ProfileLayer**
(per-agent default binding).

Full schema: [profile.yaml reference](../../reference/dsl/profile-yaml.md).

### `CapabilityProfile` — `.reyn/capability_profiles/<name>.yaml`

The named, declarative capability spec. One project can define many; a running
session may have zero or more applied simultaneously. This feeds
**ContextualLayer** (per-session dynamic binding) through composition.

## `CapabilityProfile` spec

All fields are optional; absent or `null` means unrestricted on that axis.

### Axis A — skill narrowing

| Field | Type | Semantics |
|-------|------|-----------|
| `skill_allow` | `list[str] \| null` | Skill allow-list. `null` = unconstrained. `[]` = none. |
| `skill_deny` | `list[str]` | Skill deny-list. Union across composed profiles. |

### Axis B — MCP narrowing

| Field | Type | Semantics |
|-------|------|-----------|
| `mcp_allow` | `list[str] \| null` | MCP server allow-list. `null` = unconstrained. |
| `mcp_deny` | `list[str]` | MCP server deny-list. |

### Axis C — tool narrowing

| Field | Type | Semantics |
|-------|------|-----------|
| `tool_allow` | `list[str] \| null` | Tool allow-list. `null` = unconstrained (deny-list only). |
| `tool_deny` | `list[str]` | Tool deny-list. Deny wins over allow on same name. |

### Axis D — category visibility

| Field | Type | Semantics |
|-------|------|-----------|
| `categories` | `list[str] \| null` | Categories to **keep visible**. `null` = all visible. `[]` = hide all. |

Unknown category names are a no-op (forward-compat). `visible ⊆ authorized`
holds structurally — visibility can only hide, never re-grant.

### Identity fields

| Field | Type | Default |
|-------|------|---------|
| `name` | string | required (== file stem) |
| `description` | string | `""` |

## Composition (ContextualLayer)

When multiple profiles are applied in one session, `compose_resolved` merges
them **most-restrictive-wins**:

- `*_deny` → **union** (any profile's deny wins)
- `*_allow` → **intersection** of all constraining allow-sets (`null` = ⊤,
  skipped); a value stays allowed only if every constraining profile permits it
- `excluded_categories` → **union** (any profile's hide wins)

An empty profile list → inert result, byte-identical to no profile.

## Context-auto untrusted narrowing

One profile is auto-applied while untrusted external content is live in the
active context — no explicit binding needed:

**Profile name:** `_untrusted` (built-in secure default; overridable via
`.reyn/capability_profiles/_untrusted.yaml`)

**Trigger:** any history/context entry whose meta carries `external_source=true`
(stamped by the content-fence seam at ingest).

**Built-in deny-set:** memory writes/deletes, re-delegation, sandboxed
execution, MCP install. Untrusted content can be read and reasoned about, but
cannot drive irreversible actions. Override is a deliberate loosening — a
malformed `_untrusted.yaml` falls back to the built-in (surfaced on stderr).

## Agent self-edit

An agent can update either surface at runtime without requesting extra
permissions. Both paths are within the default write zone (`.reyn/`) and are
not protected paths.

### Edit the contextual spec

**Path:** `.reyn/capability_profiles/<name>.yaml`

**Effect:** applies via ContextualLayer; composable across multiple profiles.

**Procedure:** write YAML with the desired axes. Use as ContextualLayer input
for per-session task-scoped narrowing.

### Edit the per-agent baseline

**Path:** `.reyn/agents/<agent_name>/profile.yaml`

**Effect:** applies via ProfileLayer (the agent's default spec); uses the
natural `allowed_skills` / `allowed_mcp` keys (no YAML rename).

**Verification:** `_DEFAULT_WRITE_ZONES = (".reyn",)` and
`_CANONICAL_PROTECTED_WRITE_PATHS` contains only `.reyn/approvals.yaml` and
`.reyn/index/sources.yaml`. Confirmed in `src/reyn/security/permissions/permissions.py`.

## Reload

Changes to both files take effect at **next session startup**. The files are
loaded once at session construction; running sessions use their in-memory copy.

Turn-boundary hot-reload is planned.

## Schema example

```yaml
# .reyn/capability_profiles/read-only-researcher.yaml
name: read-only-researcher
description: "Read and reason; no writes, delegation, or execution."
categories:            # keep visible
  - file
  - web
skill_allow: null      # all skills available
skill_deny: []
mcp_allow: null        # all MCP servers available
mcp_deny: []
tool_allow: null       # deny-list only
tool_deny:
  - exec__sandboxed_exec
  - memory_operation__remember_shared
  - multi_agent__delegate
```

## See also

- [Permission model § conjunctive restrict + one spec two binding adapters](permission-model.md#effective-permission-conjunctive-restrict-model) — the ∩ formula, ProfileLayer vs ContextualLayer, adapter design
- [Reference: profile.yaml](../../reference/dsl/profile-yaml.md) — AgentProfile schema (allowed_skills, allowed_mcp, default_profile mapping)
- [Concepts: multi-agent](../multi-agent/multi-agent.md) — topology and delegation (ContextualLayer consumers)
- [Reference: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`, `reyn agent list`
