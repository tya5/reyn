---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [delegation policy, default-deny delegation, _delegate profile, capability_default, delegate floor, no laundering, recursive delegate, delegation-unsafe, gateway:delegation-unsafe, reyn audit, DelegationConfig, resolved_profile_for, is_delegate, FLOORED_DENY_CLASSES]
---

# Delegation policy

The delegation policy controls the capability surface an agent receives when it
is spawned as a delegation target. By default, a delegated agent inherits the
spawner's full capability — the same as pre-policy behavior. Optionally, a
**default-deny** mode narrows every unbound delegate to a restrictive floor
without requiring per-agent topology bindings.

## Configuration

```yaml
# reyn.yaml
delegation:
  capability_default: deny   # default: inherit
```

| Value | Behavior |
|-------|----------|
| `inherit` (default) | Delegate inherits the spawner's capability surface — byte-identical to pre-policy. |
| `deny` | An unbound delegate receives the `_delegate` floor (see below). |

Only the **unbound-delegate fallback** is affected: a top-level agent and any
topology-bound delegate are unchanged regardless of this setting.

## The `_delegate` floor

When `capability_default=deny`, an unbound delegate — one spawned via the A2A
request path with no topology `capability_profile` binding — is narrowed by the
built-in `_delegate` profile.

**Built-in deny set** (same taxonomy as the `_untrusted` profile, single-sourced
from `_FLOORED_DENY_CLASSES`):

| Class | Denied tools | Rationale |
|-------|-------------|-----------|
| `re-delegation` | `multi_agent__delegate`, `delegate_to_agent` | Prevent unlimited spawning chains from an unbound delegate |
| `exec` | `exec__sandboxed_exec`, `sandboxed_exec` | Execution requires explicit operator authorization |
| `mcp-install` | `mcp__install_registry`, `mcp__install_package`, `mcp__install_local` | MCP server installation is a high-privilege, operator-controlled action |
| `memory-write` | `memory_operation__remember_shared`, `memory_operation__remember_agent`, `memory_operation__forget` | Persistence from an unbound delegate requires deliberate opt-in |

The floor is overridable: an operator file
`.reyn/capability_profiles/_delegate.yaml` replaces the built-in profile. A
malformed override falls back to the built-in (surfaced on stderr) — a typo
must not silently drop the floor.

## Binding replaces the floor (= binding is the re-grant)

A topology `capability_profile` binding **replaces** the `_delegate` floor, not
composes with it. This is the re-grant mechanism:

- **Unbound delegate** → `_delegate` floor applied.
- **Bound delegate** → topology binding replaces the floor. The bound profile
  is the full ContextualLayer for that agent; the `_delegate` floor is not
  additionally composed.

The reason: `compose_resolved` is most-restrictive-wins. Composing a floor that
denies `exec` with a profile that allows it would still deny — the floor would
be un-grantable. Instead, the registry applies the floor ONLY when the delegate
is unbound; a binding means the operator has deliberately expressed capability
for that role.

## Recursive propagation (no laundering)

The `is_delegate` flag is set on **every A2A request-path load**, regardless of
the spawner's own status. A re-granted coordinator that receives a topology
binding — and thus has `exec` re-granted — still marks its own sub-delegates
`is_delegate=True`. Those sub-delegates, if unbound, still receive the
`_delegate` floor.

**Consequence**: a re-granted coordinator cannot "launder" the floor to an
unbound sub-delegate by passing through its own wider capability. The floor
propagates to every hop in the delegation chain.

## `reyn audit` — `gateway:delegation-unsafe`

The `reyn audit` command includes a static delegation-safety rule
(`gateway:delegation-unsafe`, rule 4) that scans the project's topology and
capability profiles for re-grants of dangerous classes.

### What is scanned

**OPT-A reachability-precise scoping**: only roles with an inbound `can_send`
edge (= actual delegation targets in the A2A request path) are flagged. An
outbound-only role (e.g. a hierarchy's top coordinator that legitimately holds
`delegate_to_agent`) has no inbound delegation path and is not a delegation
target — it is not flagged, avoiding a false HIGH exit.

**`_delegate.yaml` override**: the override file is scanned unconditionally
(no reachability check needed — it is the global unbound-delegate floor).

### Findings

| Finding | Severity | Condition |
|---------|----------|-----------|
| Bound profile re-grants a class | HIGH | `re-delegation` or `exec` class permitted |
| Bound profile re-grants a class | MED | `memory-write` or `destructive-fs` class permitted |
| `_delegate.yaml` re-grants a class | HIGH / MED | Same class-to-severity mapping |
| Posture nudge | INFO | `capability_default=inherit` while any topology has a delegation edge |

The `destructive-fs` class (`delete_file`, `file__delete`) is **audit-only** —
it is not on the runtime `_delegate` floor because it is already gated by the
FILE_WRITE permission system. The audit surfaces it as a re-grant judgment for
delegate-reachable roles.

**Exit behavior**: `reyn audit` exits non-zero only on a HIGH finding — CI-safe
(a HIGH blocks a deploy; MED and INFO are informational).

### Audit classes

| Class | Severity | Tools |
|-------|----------|-------|
| `re-delegation` | HIGH | `multi_agent__delegate`, `delegate_to_agent` |
| `exec` | HIGH | `exec__sandboxed_exec`, `sandboxed_exec` |
| `mcp-install` | HIGH | `mcp__install_registry`, `mcp__install_package`, `mcp__install_local` |
| `memory-write` | MED | `memory_operation__remember_shared`, `memory_operation__remember_agent`, `memory_operation__forget` |
| `destructive-fs` | MED | `delete_file`, `file__delete` (audit-only, not on runtime floor) |

### Usage

```
reyn audit                     # scan all
reyn audit --json              # JSON output for CI pipeline consumption
```

## End-to-end flow

```
A → delegates to B
      ↓
   is_delegate=True
      ↓
   registry.resolved_profile_for("B", is_delegate=True)
      ↓
   no topology binding?
     ├── capability_default=inherit → (None, frozenset())  [pre-policy]
     └── capability_default=deny   → _delegate floor applied

B → delegates to C
      ↓
   is_delegate=True  (ALWAYS — recursive)
      ↓
   C unbound → _delegate floor  (even if B was re-granted by a binding)
```

## See also

- [Concepts: Capability profile § Default-deny delegation narrowing](capability-profile.md#default-deny-delegation-narrowing-2081) — the `_delegate` section in the capability-profile overview
- [Concepts: Capability profile](capability-profile.md) — the full ∩ model, ProfileLayer vs ContextualLayer, self-edit
- [Concepts: Multi-agent](../multi-agent/multi-agent.md) — topology, delegation, `can_send` edges
- [Reference: reyn.yaml § delegation](../../reference/config/reyn-yaml.md) — `delegation.capability_default` config
