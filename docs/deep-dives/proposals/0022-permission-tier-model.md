# FP-0022: Permission Tier Model — Formalizing the Two-Axis Framework

**Status**: **done** — LANDED 2026-05-14 (commit `61dc193`): `require_web_fetch()` + handler-level 4-layer approval + `web_search` config-deny path + tier model docs (en+ja)
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

The permission system has two axes — **usage declaration** (skill declares intent) and
**authorization** (operator/user grants access) — but neither axis is formally documented,
and individual ops apply different layers inconsistently. This proposal formalizes a
four-tier model analogous to Android's Normal/Dangerous permission distinction, and
fixes two concrete asymmetries: `web_fetch` uses only a catalog-level config gate
(bypassing the 4-layer interactive approval stack), and `web_search` has no config
restriction path at all.

---

## Motivation

### The two-axis model (currently implicit)

**Axis 1 — Usage Declaration** (`skill.md` frontmatter `permissions:` block):
The skill author declares what the skill intends to use. An undeclared op raises
`PermissionError` immediately — the skill has no intent to perform this action
(analogous to Android `SecurityException` when calling an API not declared in the manifest).

**Axis 2 — Authorization** (operator/user grants access):
Four resolution layers in `PermissionResolver._approve()`:

| Layer | Source | Who | Persistence |
|---|---|---|---|
| 1 | `reyn.yaml` `permissions.<key>: allow/deny` | Operator | Static file |
| 2 | `.reyn/approvals.yaml` | User (ALWAYS/NEVER) | Cross-session |
| 3 | `self._session[key]` in-memory | User (YES/NO) | Session only |
| 4 | Interactive prompt | User (real-time) | → Layer 2 or 3 |

### Current asymmetry

| Op | Declaration | Auth layers | Should be |
|---|---|---|---|
| `shell` | `decl.shell` required | 4 layers | ✓ Tier 3 |
| `mcp` | `decl.mcp` required | 4 layers | ✓ Tier 2 |
| `file` (outside zone) | `decl.file_*` required | 4 layers | ✓ Tier 3 |
| `web_fetch` | none | config only (Layer 1) | ✗ → Tier 1 (4 layers) |
| `web_search` | none | 0 layers (always pass) | ✗ → Tier 1 (config deny) |
| `run_skill`, `ask_user` | none | 0 layers | ✓ Tier 0 |

`web_fetch` is silently unavailable unless the operator sets `web.fetch: allow` in config.
The user never sees a prompt; the LLM doesn't know the tool exists until enabled.
This creates a poor UX (user asks the agent to look something up; the agent refuses with
no explanation) and bypasses the established approval machinery entirely.

### The Android analogy

Android distinguishes Normal permissions (auto-granted, declared in manifest) from
Dangerous permissions (require user approval at runtime). Reyn's tier model maps
directly to this:

- **Tier 0** = no manifest entry needed, no runtime gate (implicit always-on capability)
- **Tier 1** = Normal permission: no declaration required, default-allow, but operator
  can restrict via config `deny`
- **Tier 2–3** = Dangerous permission: explicit declaration required + user approval

---

## Proposed implementation

### Tier model (formal definition)

| Tier | Representative ops | Declaration | Default | Config restriction |
|---|---|---|---|---|
| 0 | `run_skill`, `ask_user` | not required | unconditional pass | not possible (would break arch) |
| 1 | `web_search`, `web_fetch` | not required | allow | ✓ `deny` blocks |
| 2 | `mcp` | required | ask (4-layer) | ✓ `allow` pre-approves |
| 3 | `shell`, `file` (outside zone) | required | ask (4-layer) | ✓ `allow` pre-approves |

Tier 0 is "unconditional pass", not "default allow" — there is no config key that could
block these ops without breaking skill execution semantics.

### Change 1 — `web_fetch`: catalog gate → handler-level `_approve()`

**`src/reyn/permissions/permissions.py`** — add method:

```python
async def require_web_fetch(self, url: str, bus: InterventionBus) -> None:
    """Tier 1 gate for web_fetch — no declaration required, full 4-layer approval."""
    if not await self._approve("web.fetch", f"web fetch: {url}", bus):
        raise PermissionError("web fetch denied")
```

**`src/reyn/op_runtime/web.py`** — add at top of `handle_web_fetch()`:

```python
if ctx.permission_resolver is not None:
    if ctx.intervention_bus is None:
        raise RuntimeError("web_fetch op requires intervention_bus on OpContext")
    await ctx.permission_resolver.require_web_fetch(op.url, ctx.intervention_bus)
```

**`src/reyn/chat/services/router_host_adapter.py`**:
- Remove `get_web_fetch_allowed()` and its call sites
- Always include `web_fetch` in the router catalog (remove conditional)

**`src/reyn/chat/router_tools.py`**:
- Remove `web_fetch_allowed` parameter and conditional include

**Default behavior after change**:
- Config unset → first use triggers interactive prompt (YES/NO/ALWAYS/NEVER)
- ALWAYS → persisted to `.reyn/approvals.yaml`; no prompt on subsequent uses
- `web.fetch: allow` → pre-approved, no prompt (existing behavior preserved)
- `web.fetch: deny` → immediate `PermissionError`

### Change 2 — `web_search`: add config `deny` path

**`src/reyn/op_runtime/web.py`** — add at top of `handle_web_search()`:

```python
if ctx.permission_resolver is not None and ctx.permission_resolver._is_config_denied("web.search"):
    raise PermissionError("web search denied by config (web.search: deny)")
```

Default behavior is unchanged (always passes). `web.search: deny` in `reyn.yaml` blocks it.
No interactive prompt needed — web search is read-only and has no side effects, so
operator `deny` is the only sensible restriction path.

### Change 3 — documentation

**`docs/concepts/permission-model.md`**:
- Add "Tier model" section with the table above
- Clarify the two-axis framework (declaration vs authorization)
- Document `web.fetch` and `web.search` config keys

---

## Target files

| File | Change |
|---|---|
| `src/reyn/permissions/permissions.py` | Add `require_web_fetch()` |
| `src/reyn/op_runtime/web.py` | Add `require_web_fetch()` call in handler; add `_is_config_denied()` in `handle_web_search()` |
| `src/reyn/chat/services/router_host_adapter.py` | Remove `get_web_fetch_allowed()`; always include `web_fetch` in catalog |
| `src/reyn/chat/router_tools.py` | Remove `web_fetch_allowed` conditional |
| `docs/concepts/permission-model.md` | Add Tier model + two-axis explanation |

---

## Dependencies

None. `_approve()` and `_is_config_denied()` already exist. `OpContext` already
has `permission_resolver` and `intervention_bus` fields (MCP handler sets the precedent).

Existing `web.fetch: allow` config entries continue to work — `_is_config_approved()`
handles them at Layer 1, short-circuiting the interactive prompt.

---

## Cost estimate

| Task | Cost |
|---|---|
| Add `require_web_fetch()` + handler call | SMALL |
| Remove router catalog gate | SMALL |
| Add `web_search` deny check | SMALL |
| Update `docs/concepts/permission-model.md` | SMALL |
| **Total** | **SMALL** |

All changes are additive or subtractive in small, isolated call sites. No protocol
changes; existing approvals in `.reyn/approvals.yaml` continue to work.

---

## Related

- `src/reyn/chat/services/router_host_adapter.py` — `get_web_fetch_allowed()` to remove
- `src/reyn/permissions/permissions.py` — `_approve()`, `_is_config_denied()` to reuse
- `docs/concepts/permission-model.md` — document to extend
- FP-0021 (`0021-event-log-audit-completeness.md`) — filed in the same session
- Android Normal/Dangerous permission model — design precedent
