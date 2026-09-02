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
| **IN-set** (runtime-mutable) | `.reyn/config/mcp.yaml`, `.reyn/config/cron.yaml`, `.reyn/config/hooks.yaml` | Hot-reload at turn boundary |
| **OUT-set** (restart-only) | `reyn.yaml` (security / permissions / sandbox / budget / loop valve) | Process restart only |

The boundary is structural: `load_hot_reload_config` opens only the `.reyn/config/*.yaml`
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

- `source` — the triggering caller: `"operator"` (`/reload`), `"llm_op"`
  (`hooks_add`), `"spawn_refresh"` (`apply_all`, the ephemeral/spawn
  action-boundary), or an install op's own label (`"pipeline_install"`,
  `"skill_install"`, `"presentation_install"`, `"mcp_install"`,
  `"mcp_install_local"`)
- `components` — list of changed seam names
- `failed` — list of seam names that raised
- `detail` (#3636, optional, default `None`) — a single-entity qualifier an
  install call site may supply (e.g. the specific pipeline/skill/server name),
  threaded through `HotReloader.apply_now`/`request_reload` via
  `dispatch_install_reload`'s `detail=` kwarg. Without it, two DIFFERENT
  installs of the same `source` kind in quick succession (e.g. a plugin
  bundling two pipelines) emit two correct-but-indistinguishable
  `config_reloaded` events that collapse to byte-identical `state_change`
  history text — an adjacent-duplicate-shaped artifact of lost resolution,
  not an actual double-write (see the session.py
  `_STATE_CHANGE_EVENT_MAPPINGS["config_reloaded"]` formatter, which folds
  `detail` into the summary when present).

Every config change is an evented, replay-capable state change (P6).

### Boot resilience

An absent `.reyn/` directory or missing file yields `{}` for that component — a
no-op reload, never an error. A reload can never crash the session.

## Per-component reapply seams

Five seams are registered on the `HotReloader` at session construction. All five
run on every reload:

| Seam | What it does |
|------|--------------|
| `cron` | Adds / replaces present jobs (idempotent by name). **Removal-diff**: jobs tracked in `_runtime_cron_names` that are absent from the re-read `.reyn/config/cron.yaml` are unscheduled. Startup (`reyn.yaml`) cron jobs are never removable. |
| `mcp` | Re-probes MCP servers via the existing turn-boundary refresh chain. Reports whether the in-memory tool cache changed. |
| `per_agent_capability` | Re-reads `.reyn/agents/<name>/profile.yaml` and updates `allowed_mcp` on the three holders the Session owns (session / skill_runner / router_host). |
| `new_agent` | Confirming no-op: agent discovery is filesystem-live (the `AgentRegistry` walks `.reyn/agents/` per call), so a newly added agent is already visible without a reload step. Kept as an explicit seam for accounting. |
| `hooks` | Re-reads global `.reyn/config/hooks.yaml` + per-agent `.reyn/agents/<name>/hooks.yaml`, re-combines with the fixed startup and trusted-per-agent layers, and swaps the hook dispatcher's registry. |

## Hooks 5-layer COMBINE

The hook registry is built additively from five layers, in
[`HOOK_ORIGIN_ORDER`](../../reference/config/reyn-yaml.md#hooks-block)'s
own order:

| Layer | File | Set | On reload |
|-------|------|-----|-----------|
| **startup** | `reyn.yaml` | OUT-set | Captured once at boot; never re-read |
| **runtime** | `.reyn/config/hooks.yaml` | IN-set | Re-read on every reload |
| **trusted-per-agent** (#5505) | `.reyn/config/agents/<name>/hooks.yaml` | neither — boot-only, not in the IN-set | Captured once at boot; never re-read |
| **per-agent** | `.reyn/agents/<name>/hooks.yaml` | IN-set | Re-read on every reload |
| **per-session** (#2285) | `<session_state_dir>/hooks.yaml` | session-local, session-lifetime | Re-read on every reload |

The COMBINE is additive: `startup ∪ runtime ∪ trusted-per-agent ∪ per-agent ∪
per-session`. A removed hook is absent from the rebuilt registry — removal is
handled by reconstruction (no explicit remove step) — except
**trusted-per-agent**, which is deliberately NOT reconstructed from the file on
a reload (see below); removing that file only takes effect on restart.

**trusted-per-agent** (`.reyn/config/agents/<name>/hooks.yaml`) carries
**only** the permission-bearing per-hook keys (`write_paths`/`subprocess`/
`network`) — the grant mechanism #5356 (below) otherwise leaves with no
per-agent path at all. See [Concepts: Hooks §
Sandbox](hooks.md#which-layer-may-grant-the-three-sandbox-axes) for the full
rationale, including the two separate senses of "trusted" this layer
carries.

**Per-layer boot resilience.** The trusted startup layer (`reyn.yaml`,
operator-controlled) must load — a failure is fail-loud. The
**trusted-per-agent** layer gets the SAME fail-loud posture, at boot only
(architect ruling: a permission-bearing layer silently dropping mid-session
is worse than a refused boot) — unlike every OTHER post-startup layer below,
which is try-added independently:

- A bad runtime layer keeps every other good layer; the bad layer is dropped + warned.
- A bad per-agent layer keeps every other good layer; the bad layer is dropped + warned.
- A bad per-session layer keeps every other good layer; the bad layer is dropped + warned.

On the reload path, validate-before-apply also rejects a bad runtime layer up
front (defense-in-depth). **trusted-per-agent** is never part of validate-
before-apply — it is outside the IN-set entirely.

## The per-agent self-grant restriction (#5356)

`write_paths`/`subprocess`/`network` are REJECTED outright — a load-time
`HookConfigError`, not a silent drop — when declared at **per-agent** or
**per-session**: an agent can already write either file via the ordinary
file-write op, so a grant declared there is a confused-deputy self-grant,
not an operator's expressed will. The same three keys are honored at
**startup**, **runtime**, and **trusted-per-agent** — none of those three is
agent-writable.

## Triggers

### Operator: `/reload`

The `/reload` slash command schedules a reload at the next turn boundary.

```
/reload
```

The OUT-set (`reyn.yaml`) is never touched. Responds with a confirmation that
the reload is scheduled and will apply at the next turn boundary.

### Agent self-reload: `hooks_add`

The `hooks_add` LLM-op writes a push hook and schedules a reload. The hook takes
effect at the next turn boundary via the `hooks` reapply seam.

**Session-local write target (#4215①, superseding #2088's scope-aware
write).** The write lands at exactly ONE fixed path, chosen by the CALLING
session's own identity — never by LLM input: every session (named agent or
default, "main" or spawned) writes its OWN per-session layer
`<session_state_dir>/hooks.yaml` — the same file the per-session COMBINE
layer above already reads.

#2088 closed the #2073 follow-up (operator-authored per-agent hooks were
read+combined since #2073's per-agent-hooks add-on, but `hooks_add` itself
always wrote the global layer) by giving a named agent its own per-agent
write target — but that target was still SHARED across every session of
that agent, and the default/unnamed agent still wrote the shared GLOBAL
layer. #4215① closes both remaining leaks: hooks are reyn's one *reactive*
corner (the OS acting on someone else's registration rather than the
agent's own decision), so a session's self-expanded hooks must never leak
into, or be leaked into by, a sibling session. Precedence between the
per-session layer and every other layer (startup, global runtime,
per-agent) is ADDITIVE, not override — see the COMBINE above. The
operator-facing global and per-agent layers are unchanged and still read;
`hooks_add` simply no longer writes to either.

`hooks_add` parameters:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `on` | yes | Lifecycle point: `turn_start`, `turn_end`, `session_start`, `session_end` |
| `message` | yes | Push message (Jinja2 template allowed) |
| `wake` | no | `true` → starts a new turn (self-continuation); `false` → rides along as context with the next turn. Default `true`. |
| `push_when` | no | Jinja2 → bool guard; the push is skipped when this renders false. |
| `name` | no | Label surfaced as `[hook:name]` attribution prefix in history. |

The tool is write-gated: the calling workflow must declare `hooks_add` in
`permissions.tool`, and the capability profile `tool_deny` can deny it.

**Which reloader instance gets triggered.** Separately from which *file*
`hooks_add` writes to (above), the reload it schedules always runs on the
*calling session's own* `HotReloader` (threaded via `ToolContext.hot_reloader`)
— never a process-wide "last constructed session" fallback. This differs from
cron's own reload path, which does use that fallback (see its own caveat
elsewhere in this doc); in a multi-agent setup, agent A calling `hooks_add`
reloads A, regardless of which session was constructed most recently.

**Validates before writing, not just before applying.** An invalid hook body
(e.g. an unrecognised `on:` value) is rejected with nothing written to disk —
this is a separate check from the reload-time "Validate-before-apply" safety
story below, which guards a persisted-but-malformed file; this one guards the
write itself.

**Re-adding an identical hook is a no-op**, not an accumulating duplicate —
an exception to the general "idempotency is the caller's responsibility"
posture (see [reliability engineering](../agent-engineering/reliability-engineering.md)):
`hooks_add` compares the new hook against existing entries and skips the
write on an exact match.

## Safety story

Hot-reload is safe-by-construction through five layers:

1. **Write-gate by construction.** `load_hot_reload_config` never opens `reyn.yaml`.
   `hooks_add` hardcodes the write target to exactly one path — the calling
   session's OWN per-session layer `<session_state_dir>/hooks.yaml` (#4215①,
   chosen by the calling session's own identity) — never derived from LLM
   input. An LLM-triggered reload structurally cannot touch the OUT-set, the
   global runtime layer, nor any other session's or agent's layer.
2. **Validate-before-apply.** A malformed IN-set rejects the whole reload atomically —
   no half-apply, live config unchanged.
3. **Boot resilience.** Per-layer independent try-add for untrusted layers: a bad
   layer drops + warns without crashing boot or dropping sibling layers.
4. **Sandbox.** The sandbox guards shell hook execution. (Pre-#5561 this
   layer also cited a `wake:true`-loop cap, `safety.loop.max_hook_driven_turns`
   — that valve is retired; see [Concepts: hooks § loop valve](hooks.md#loop-valve)
   for the current bounding mechanisms.)
5. **Capability-profile deny.** `tool_deny: [hooks_add]` in a capability profile
   prevents the agent from adding hooks — the feature can be disabled per-agent via
   the ∩ model. See [Capability profile](capability-profile.md).

## See also

- [Concepts: Hooks](hooks.md) — the 6 lifecycle points, push/shell schemes, wake-loop behavior
- [Concepts: Capability profile](capability-profile.md) — `tool_deny` gate for `hooks_add`; per-agent-capability reapply seam
- [Concepts: Permission model](permission-model.md) — the ∩ model and the write-gate boundary
- [Reference: reyn.yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — startup hooks config (OUT-set)
- [Reference: Events](../../reference/runtime/events.md) — `config_reloaded` P6 event
