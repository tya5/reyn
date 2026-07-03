---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [config hot-reload, hot-reload, IN-set, OUT-set, HotReloader, hooks_add, /reload, mcp.yaml, cron.yaml, hooks.yaml, reyn.yaml, turn boundary, config_reloaded, reapply seam, hooks layer, per-agent hooks, write-gate]
---

# Config hot-reload

Reyn's config is split into two sets with different mutability rules. The
hot-reload mechanism re-reads the runtime-mutable set at a safe-point without a
process restart.

## IN-set vs OUT-set (the write-gate boundary)

| Set | Files | Mutable at… |
|-----|-------|-------------|
| **IN-set** (runtime-mutable) | `.reyn/mcp.yaml`, `.reyn/cron.yaml`, `.reyn/hooks.yaml` | Hot-reload at turn boundary |
| **OUT-set** (restart-only) | `reyn.yaml` (security / permissions / sandbox / budget / loop valve) | Process restart only |

The boundary is structural: `load_hot_reload_config` opens only the `.reyn/*.yaml`
IN-set files. A hot-reload — and any LLM-op that triggers one — can never touch
the OUT-set, because the loader never opens those files.

## HotReloader mechanics

### Turn-boundary safe-point (timing-B)

A trigger calls `request_reload(source=…)`, which **schedules** the reload but
does not apply it immediately. The reload applies at `apply_pending()`, called
at the turn boundary (finish-reason=stop — the `turn_end` safe-point). Multiple
triggers within one turn collapse into a single apply: **1 turn = 1 config
snapshot**; the next turn runs under the new config.

### Validate-before-apply

Before any reapply seam runs, the IN-set is checked structurally. A malformed
IN-set (bad cron job shape, malformed hooks YAML) **rejects the whole reload** —
no seam runs, the live config is unchanged. The `config_reloaded` P6 event is not
emitted on rejection (no state change occurred).

### P6 event

On a successful apply, `config_reloaded` is emitted with:

- `source` — `"operator"` (from `/reload`) or `"llm_op"` (from `hooks_add`)
- `components` — list of changed seam names
- `failed` — list of seam names that raised

Every config change is an evented, replay-capable state change (P6).

### Boot resilience

An absent `.reyn/` directory or missing file yields `{}` for that component — a
no-op reload, never an error. A reload can never crash the session.

## Per-component reapply seams

Five seams are registered on the `HotReloader` at session construction. All five
run on every reload:

| Seam | What it does |
|------|--------------|
| `cron` | Adds / replaces present jobs (idempotent by name). **Removal-diff**: jobs tracked in `_runtime_cron_names` that are absent from the re-read `.reyn/cron.yaml` are unscheduled. Startup (`reyn.yaml`) cron jobs are never removable. |
| `mcp` | Re-probes MCP servers via the existing turn-boundary refresh chain. Reports whether the in-memory tool cache changed. |
| `per_agent_capability` | Re-reads `.reyn/agents/<name>/profile.yaml` and updates `allowed_skills` / `allowed_mcp` on the three holders the Session owns (session / skill_runner / router_host). |
| `new_agent` | Confirming no-op: agent discovery is filesystem-live (the `AgentRegistry` walks `.reyn/agents/` per call), so a newly added agent is already visible without a reload step. Kept as an explicit seam for accounting. |
| `hooks` | Re-reads global `.reyn/hooks.yaml` + per-agent `.reyn/agents/<name>/hooks.yaml`, re-combines with the fixed startup layer, and swaps the hook dispatcher's registry. |

## Hooks three-layer COMBINE

The hook registry is built additively from three layers, in order:

| Layer | File | Set | On reload |
|-------|------|-----|-----------|
| **startup** | `reyn.yaml` | OUT-set | Captured once at boot; never re-read |
| **runtime** | `.reyn/hooks.yaml` | IN-set | Re-read on every reload |
| **per-agent** | `.reyn/agents/<name>/hooks.yaml` | IN-set | Re-read on every reload |

The COMBINE is additive: `startup ∪ runtime ∪ per-agent`. A removed hook is
absent from the rebuilt registry — removal is handled by reconstruction (no
explicit remove step).

**Per-layer boot resilience.** The trusted startup layer (`reyn.yaml`, operator-
controlled) must load — a failure is fail-loud. Each untrusted layer (runtime,
per-agent) is try-added independently:

- A bad runtime layer keeps `startup ∪ per-agent`; the bad layer is dropped + warned.
- A bad per-agent layer keeps `startup ∪ runtime`; the bad layer is dropped + warned.

On the reload path, validate-before-apply also rejects a bad runtime layer up
front (defense-in-depth).

## Triggers

### Operator: `/reload`

The `/reload` slash command schedules a reload at the next turn boundary.

```
/reload
```

The OUT-set (`reyn.yaml`) is never touched. Responds with a confirmation that
the reload is scheduled and will apply at the next turn boundary.

### Agent self-reload: `hooks_add`

The `hooks_add` LLM-op writes a push hook to `.reyn/hooks.yaml` and schedules a
reload. The hook takes effect at the next turn boundary via the `hooks` reapply
seam.

`hooks_add` parameters:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `on` | yes | Lifecycle point: `turn_start`, `turn_end`, `session_start`, `session_end`, `skill_start`, `skill_end`, `task_start`, `task_end` |
| `message` | yes | Push message (Jinja2 template allowed) |
| `wake` | no | `true` → starts a new turn (self-continuation, bounded by `safety.loop.max_hook_driven_turns`); `false` → rides along as context with the next turn. Default `true`. |
| `push_when` | no | Jinja2 → bool guard; the push is skipped when this renders false. |
| `name` | no | Label surfaced as `[hook:name]` attribution prefix in history. |

The tool is write-gated: the calling workflow must declare `hooks_add` in
`permissions.tool`, and the capability profile `tool_deny` can deny it.

## Safety story

Hot-reload is safe-by-construction through five layers:

1. **Write-gate by construction.** `load_hot_reload_config` never opens `reyn.yaml`.
   `hooks_add` hardcodes the write target to `.reyn/hooks.yaml` — the path is never
   derived from LLM input. An LLM-triggered reload structurally cannot touch the
   OUT-set.
2. **Validate-before-apply.** A malformed IN-set rejects the whole reload atomically —
   no half-apply, live config unchanged.
3. **Boot resilience.** Per-layer independent try-add for untrusted layers: a bad
   layer drops + warns without crashing boot or dropping sibling layers.
4. **Sandbox + loop valve.** Hook `wake:true` loops are bounded by
   `safety.loop.max_hook_driven_turns`. The sandbox guards shell hook execution.
5. **Capability-profile deny.** `tool_deny: [hooks_add]` in a capability profile
   prevents the agent from adding hooks — the feature can be disabled per-agent via
   the ∩ model. See [Capability profile](capability-profile.md).

## See also

- [Concepts: Hooks](hooks.md) — the 8 lifecycle points, push/shell schemes, wake-loop behavior
- [Concepts: Capability profile](capability-profile.md) — `tool_deny` gate for `hooks_add`; per-agent-capability reapply seam
- [Concepts: Permission model](permission-model.md) — the ∩ model and the write-gate boundary
- [Reference: reyn.yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — startup hooks config (OUT-set)
- [Reference: Events](../../reference/runtime/events.md) — `config_reloaded` P6 event
