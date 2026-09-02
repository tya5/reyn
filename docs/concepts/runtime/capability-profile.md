---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [capability profile, agent profile, allowed_mcp, tool_allow, tool_deny, mcp_allow, mcp_deny, categories, category visibility, ContextualLayer, ProfileLayer, self-edit, untrusted narrowing]
---

# Capability profile

The capability profile system is the unified narrowing primitive across the
`mcp` / `tool` / `category` capability axes. It separates the
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
- `allowed_mcp` — MCP server allowlist (maps internally to `mcp_allow`)

`AgentProfile.default_profile()` converts these keys to a `CapabilityProfile`
at runtime — no user-facing rename, same semantics. This feeds **ProfileLayer**
(per-agent default binding).

Full schema: see profile.yaml.

### `CapabilityProfile` — `.reyn/capability_profiles/<name>.yaml`

The named, declarative capability spec. One project can define many; a running
session may have zero or more applied simultaneously. This feeds
**ContextualLayer** (per-session dynamic binding) through composition.

## `CapabilityProfile` spec

All fields are optional; absent or `null` means unrestricted on that axis.

### Axis A — MCP narrowing

| Field | Type | Semantics |
|-------|------|-----------|
| `mcp_allow` | `list[str] \| null` | MCP server allow-list. `null` = unconstrained. |
| `mcp_deny` | `list[str]` | MCP server deny-list. |

A profile YAML left over from before the SKILL axis was removed may still
carry `skill_allow`/`skill_deny` keys — the loader ignores them silently
(forward-compat) rather than rejecting the file. There is no warning that
those keys have stopped doing anything; an old profile file is not a signal
that skill capability is being narrowed.

### Axis B — tool narrowing

| Field | Type | Semantics |
|-------|------|-----------|
| `tool_allow` | `list[str] \| null` | Tool allow-list. `null` = unconstrained (deny-list only). |
| `tool_deny` | `list[str]` | Tool deny-list. Deny wins over allow on same name. |

### Axis C — category visibility

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

## Advertisement and enforcement read one source

A narrowed tool is **both** withheld from the LLM's tool catalog **and** rejected
if called anyway. Both halves key on the same resolved `ContextualPermission`:

| Half | What it does | Why it exists |
|------|--------------|---------------|
| Advertisement | The tool is absent from the `tools=` payload the model receives | The model never spends a turn on a capability it cannot have |
| Enforcement | A call to it returns `tool_excluded` | The model can name an unadvertised tool anyway — directly, or wrapped in `invoke_action` |

**Hiding is not denying.** The two are not redundant and neither substitutes for
the other: a model that names a tool absent from its catalog still reaches
dispatch, which is how an excluded web tool once executed. Enforcement is the
boundary; advertisement is the presentation that keeps the two agreeing.

The agreement is re-established whenever the narrowing changes mid-turn — at the
`iteration` rung of the untrusted narrowing below, it can engage between rounds,
and the round after it engages is advertised against the new narrowing, not the
turn's opening one.

### The deny names which narrowing fired

A capability can be narrowed by any of seven things — a topology binding, the
`_delegate` floor, a per-session capability config, the ⊆-parent cap, the
`/visibility` override, the run's `exclude_tools` list, or the `_untrusted` context
narrowing. Composition folds them into one ∩ term, which is what the gate
evaluates and which by itself cannot say *which* of the seven rejected a given
name.

So each term carries a `NarrowingOrigin` — three parts, all required:

| Part | Answers |
|------|---------|
| `label` | Which narrowing this is, named so it can be looked up |
| `cause` | Why it is currently active |
| `lifts_when` | What would remove it — a condition, a config key, or both |

The composed term keeps its input terms, so the deny site walks them and reports
the origin of the first one that rejects the name. One shared builder
(`contextual_deny_message`) produces the text for every deny site — the
router-loop tool gate, `require_tool`, and `require_mcp` — so the three cannot
drift into differently-informative answers to the same question.

**This is the message the model receives**, not only what the Tool tab renders. A
deny that lists the *candidate* narrowings without naming the one that fired is
not decision-enabling: the reader still has to guess, and an agent handed that
string can neither explain the loss nor act to undo it. The Tool tab (below) is
the operator's view of the same facts; it is not a substitute for putting them in
the deny, because reaching the tab requires already knowing something is wrong.

`invoke_action` is the one deliberate exception on the advertisement side: it
carries its real target in `action_name`, so what it resolves to is knowable only
at call time. The wrapper is therefore always advertised and always unwrapped
before the gate decides — withholding the wrapper under a `tool_allow` list would
remove the only route to every *allowed* action.

## Context-auto untrusted narrowing (opt-in, default off)

One profile is applied while untrusted external content is live in the active
context. It is **off by default** and engages only when the operator asks for it:

**Enable:** `safety.threat_scan.capability_narrowing` in `reyn.yaml` — one ordered
ladder of three settings, not an enable flag plus a granularity flag:

| Setting | Behaviour |
|---------|-----------|
| `off` (default) | The narrowing never engages. An agent keeps the capabilities it started the session with, whatever enters its context. |
| `turn` | While external content is live, the `_untrusted` profile applies, resolved at each turn boundary — so content arriving mid-turn narrows from the *next* turn. |
| `iteration` | As `turn`, and re-resolved at every router-loop iteration, so content arriving in round *N* narrows round *N+1* of the same turn. Monotonic within the turn (see below). |

**Why off by default.** The narrowing removes capabilities *mid-session*, and the
agent that loses them has no way to see why: the reported symptom was a capability
that worked at the start of a session, stopped working after a web fetch, and an
agent that could not explain the loss. Predictability is the default posture and
security hardening is opted into; a mechanism that silently changes what an agent
can do is exactly the kind that has to be asked for. Legibility at the point of
denial (above) is what makes the opted-in state workable rather than merely on.

This is the CAPABILITY half of the same defense whose CONTENT half is the
`safety.threat_scan` fence + scan — which is why the setting lives in that block
and not next to the loop caps.

**Profile name:** `_untrusted` (built-in deny-set; overridable via
`.reyn/capability_profiles/_untrusted.yaml`). The two surfaces answer different
questions: the config setting decides *whether* the narrowing runs, the profile
file decides *what it denies when it runs*. This is the same split
`delegation.capability_default` + `_delegate.yaml` already uses.

They are not fully disjoint in *effect* — an override with an empty `tool_deny`
neuters the narrowing just as `off` does, and is indistinguishable from outside.
That is the pre-existing "an override is a deliberate loosening" route, not a
second switch introduced alongside the setting, and it behaves identically on the
delegate axis. The setting is the one an operator should reach for: it is what the
deny message names, and turning the mechanism off is a different intent from
declaring an empty deny-set.

**Trigger (once enabled):** any history/context entry whose meta carries
`external_source=true` (stamped by the content-fence seam at ingest). Two seams
stamp it today: an external peer's `ask_user` answer (A2A / webhook), and the
result of any tool declaring `returns_external_content` (e.g. web fetch) — both
land in `self.history` meta, and the narrowing check reads
`Session._untrusted_taint_active` — **resident-ness is a resource concern,
not the decision criterion**: whether an entry currently happens to be
resident in memory is orthogonal to whether it is still logically part of
the active conversation, and this state's job is the latter, never the
former. So the very next dispatch after an external tool-result lands is
already narrowed, and the narrowing self-clears once that entry **compacts
out** — a semantic operation (its content is genuinely replaced by a summary
and leaves the model's context) — never merely because it stopped being
resident. Under `iteration` the engagement is additionally *latched* for the
rest of the turn, so a compaction that evicts the tainted entry mid-turn
cannot launder the taint away and recover the capability before the turn
ends.

**#5276: detection lives on the mutation side, not the read side.**
`_untrusted_taint_active` is maintained incrementally by
`Session._append_history` — the single mouth every history write funnels
through — rather than re-derived by a full `metas_have_untrusted` scan on
every read (owner-measured py-spy: ~60 reads/sec from the status panel, live
gate and Tool tab combined, none of which change the state). An ordinary
append only ever checks the ONE new entry (O(1)) and can only ever ADD
taint; the bounded `metas_have_untrusted` scan over the logical ACTIVE
window (every entry with `seq` above the compaction watermark) still runs,
but only at the rare event that can retroactively RETRACT the taint — a
compaction watermark advance (a `role="summary"` append) — plus at the two
paths that replace `self.history` wholesale without going through
`_append_history` at all (`load_history`, `restore_state`). Every READ still
calls `Session._ephemeral_contextual_for_turn()` fresh, exactly as before —
this is not a read-side cache with its own staleness window; it is the same
always-fresh detector, just backed by an O(1) state read instead of an
O(active-window) scan.

**#4387/#4468: a second, independent latch closing a resource/semantic role
crossing.** Session.history now also has a resident-BYTE cap (#4387) — a
RESOURCE-role operation (#4431's role split), unrelated to compaction. It can
evict an entry that is still logically active (`seq` above the compaction
watermark) purely because memory is tight, well before compaction (the only
SEMANTIC-role operation meant to retire an entry) would have folded it away —
the entry stays durable in `history.jsonl` and reloads on demand, so it has
not actually left the conversation. Without a fix, a scan that reads only
resident entries would let this resource-role operation silently decide a
semantic question it has no business deciding — so `_evict_oldest_resident_
entries` latches the highest `seq` among any evicted entry that carried the
untrusted marker (`Session._max_evicted_untrusted_seq`), and the scan ORs
that latch in alongside the in-flight one. Deliberately keyed to the SAME
extinction trigger as everything else on this page — the compaction
watermark, never "no longer resident" — so eviction can set the latch but
only compaction can clear it; tying it to residency instead would just
reproduce this exact bug on the latch's own side. This latch is monotone and
never blocks or
delays eviction itself (no DoS surface from an attacker stuffing untrusted
content to make eviction stall) — it self-clears the identical way the live
scan does, the moment compaction's own watermark advances past the latched
`seq`.

**Built-in deny-set:** memory writes/deletes, re-delegation, sandboxed
execution, MCP / skill / pipeline install, session and agent spawn, pipeline run.
Untrusted content can be read and reasoned about, but cannot drive irreversible
actions. Override is a deliberate loosening — a malformed `_untrusted.yaml` falls
back to the built-in (surfaced on stderr).

**The single opt-in read.** `Session._ephemeral_contextual_for_turn` is the one
place the setting is consulted. The live gate, the advertisement filter and the
Tool tab all derive from that one method, so they are engaged together or
disengaged together and cannot disagree about whether the mechanism is on.

**Where you see it:** the Tool tab of the CUI's bottom drawer marks such a tool
`[--] <name>  · denied while untrusted content is in context` — the *condition*,
because the condition is also the remedy (it lifts when that entry compacts out;
there is no profile to edit). That is a different row from a durable envelope
denial, which reads `· denied by capability profile`, and different again from a
`/visibility`-off row, which is user-flippable and carries a toggle. Three states,
three renderings.

The tab derives the row from the live conversation at read time — the same
`_ephemeral_contextual_for_turn()` call the gate makes, never a value latched
at turn start (see #5276 above for how that read is now backed by O(1)
mutation-side state rather than a per-read scan, without changing this
freshness property at all) — and the open pane is rebuilt on every frame, so
a row disappears as soon as its cause does. That liveness is what makes
showing a per-turn narrowing in a status surface honest rather than a stale
claim wearing an authoritative face.

## Default-deny delegation narrowing

A second built-in profile is auto-applied to a **delegated** agent when the
operator opts into strict delegation:

**Profile name:** `_delegate` (built-in restrictive default; overridable via
`.reyn/capability_profiles/_delegate.yaml`). The name is decoupled from
`_untrusted` (delegate-spawn vs untrusted-content are distinct contexts), but
the default deny-set is the **same single-sourced taxonomy** — so operators tune
delegate-deny independently.

**Trigger:** `delegation.capability_default: deny` in reyn.yaml AND the agent is
an **unbound delegate** — spawned by another agent's delegation (the A2A request
path), with no topology `capability_profile` binding.

**Effect:** the unbound delegate resolves to the `_delegate` floor instead of no
narrowing. A topology binding **replaces** the default (the binding is the
re-grant — composition is most-restrictive-wins and cannot re-grant). The
default-deny propagates **recursively**: every delegation hop marks the target a
delegate, so a re-granted coordinator's own unbound sub-delegate is still
default-denied (no laundering).

`delegation.capability_default: inherit` (the default) keeps a delegate
inheriting the spawner's surface.

**Audit:** `reyn audit` (`gateway:delegation-unsafe`) flags, per dangerous class,
a delegate-reachable bound profile (or the `_delegate.yaml` override) that
re-grants a class (re-delegation / exec = HIGH; memory-write / destructive-FS =
MED), and nudges (INFO) when `capability_default=inherit` while a topology
permits delegation.

Full mechanism: [Concepts: Delegation policy](delegation-policy.md) — config, recursive propagation, binding-replaces semantics, audit classes, and OPT-A reachability scoping.

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
natural `allowed_mcp` key (no YAML rename).

**Verification:** `_DEFAULT_WRITE_ZONES = (".reyn",)` and the canonical
protected-write list (`_canonical_protected_write_paths()`,
`src/reyn/security/permissions/permissions.py`) covers `.reyn/approvals.yaml`
(legacy, still migrated once) and `.reyn/approvals.jsonl` (the live ledger,
#5153/#5173).

## Reload

Both surfaces support **turn-boundary hot-reload** (live, no restart needed):

- **ContextualLayer** — changes to `.reyn/capability_profiles/<name>.yaml` are
  picked up by the `per_agent_capability` reapply seam, which re-reads the
  `AgentProfile` and updates `allowed_mcp` on all three holders the Session
  owns (session / skill_runner / router_host).
- **ProfileLayer** — changes to `.reyn/agents/<name>/profile.yaml` are reloaded
  by the same seam.

Both files are IN-set (`.reyn/*.yaml` grain). Trigger a reload with `/reload` or
via the `hooks_add` LLM-op. See [Concepts: Config hot-reload](config-hot-reload.md)
for the full reload cycle (timing-B safe-point, validate-before-apply, P6 event).

The per-agent hooks layer (`.reyn/agents/<name>/hooks.yaml`) is also reloaded at
the same turn boundary via the `hooks` reapply seam — the `hooks` COMBINE
re-reads startup + runtime + per-agent layers on every reload.

## Schema example

```yaml
# .reyn/capability_profiles/read-only-researcher.yaml
name: read-only-researcher
description: "Read and reason; no writes, delegation, or execution."
categories:            # keep visible
  - file
  - web
mcp_allow: null        # all MCP servers available
mcp_deny: []
tool_allow: null       # deny-list only
tool_deny:
  - exec
  - remember_shared
  - run_prompt
  - send_to_session
```

**Each tool has exactly one invocable name.** #3429 removed the second,
catalog-qualified spelling every action used to also carry (e.g. a former
`file__read` alongside `read_file`) — a `tool_deny` entry now denies exactly
the name it names, with no expansion step. Copying a name out of the model's
current tool list is safe and complete: there is no other route to the same
operation under a different spelling to separately deny.

## See also

- [Permission model § conjunctive restrict + one spec two binding adapters](permission-model.md#effective-permission-conjunctive-restrict-model) — the ∩ formula, ProfileLayer vs ContextualLayer, adapter design
- [Concepts: multi-agent](../multi-agent/multi-agent.md) — topology and delegation (ContextualLayer consumers)
- [Reference: reyn agent CLI](../../reference/cli/agent.md) — `reyn agent new`, `reyn agent list`
