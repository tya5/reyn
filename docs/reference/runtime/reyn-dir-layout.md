# The `.reyn/` directory layout

This is the canonical reference for what lives under a project's `.reyn/` directory:
the five-way classification of every subtree, which subtrees are **recovery-core**
(captured + restored by time-travel), the **write-gate** rule a workflow author hits, and
where a new subsystem should put its data.

`.reyn/` holds Reyn's own plumbing under the project root. The organizing principle is
**ownership + recovery role**: Reyn time-travels the state it *authors* and that *affects
its in-memory runtime* — not the user's project files, and not operator-owned config.

> **Rewind mechanics** (how recovery-core is reconstructed — WAL replay + snapshot
> generations, seq addressing, rewind-record append, atomicity) live in
> [Time travel](../../concepts/runtime/time-travel.md). This doc owns *what is in `.reyn/`*;
> that doc owns *how rewind works*.

## Classification

A `.reyn/` subtree is **recovery-core** iff it (1) is authored by the run (agent/runtime,
not the operator) **and** (2) affects in-memory runtime state that recovery reconstructs.
Everything else is excluded, by one of four reasons:

| Category | Handling on rewind / recovery | Subtrees |
|---|---|---|
| **recovery-core** | captured + restored (reconstructed) | `state/`, `config/` |
| **persist** (knowledge / decisions) | survives rewind — never reverted | `memory/`, `approvals.yaml` |
| **audit** (write-only record) | kept as a record, never restored | `events/`, `traces/`, `logs/`, `audit-trail/`, `tool-results/`, `media/` |
| **cache** (derived) | rebuilt after restore | `cache/` (`index/` — includes the `actions` source since FP-0057 Phase 0 — `registry-cache/`, `*_cursor`, `budget_checkpoint.json`) |
| **outside** (operator/user-owned) | not Reyn-managed for time-travel | `reyn.yaml`, `secrets.env`, `oauth_tokens.json`, `capability_profiles/` |

## Canonical layout

```
.reyn/
├── state/                  RECOVERY-CORE — run-authored, reconstructs in-memory state
│   ├── wal.jsonl           the WAL (append-only, seq'd) — the recovery TRUTH
│   ├── run_registry.json   A2A's async-run store — standalone snapshot, NOT recovery-core (see below)
│   └── budget_ledger.jsonl the cost ledger
├── agents/<name>/state/    RECOVERY-CORE (per-agent) — reconstructed alongside the WAL
│   ├── snapshot.json       the agent's runtime snapshot (a derived projection of the WAL)
│   ├── generations/        snapshot generations (gen-<seq>.json) — the PITR base
│   └── sessions/<sid>/     per-spawned-session snapshot + generations
├── config/                 RECOVERY-CORE — agent-managed registries (reconstructed by replay)
│   ├── mcp.yaml            MCP servers   (mcp_install / mcp_drop_server)
│   ├── cron.yaml           cron jobs     (cron_register / …)
│   ├── hooks.yaml          push hooks    (hooks_add)
│   ├── integrations.yaml   integrations
│   └── index/sources.yaml  index source manifest (index ops)
├── memory/                 PERSIST — agent knowledge; survives rewind, never reverted
├── approvals.yaml          PERSIST — user-authored permission grants; survive rewind
├── events/ traces/ logs/   AUDIT — append-only forensic record; never restored
│   audit-trail/ tool-results/ media/
├── cache/                  DERIVED — rebuilt after restore
│   ├── index/              rag index data (sqlite), one dir per source —
│   │   └── actions/        includes the tool-use action catalog since
│   │                       FP-0057 Phase 0 (was the separate action_index/
│   │                       implementation pre-consolidation; clean-break,
│   │                       no migration — see `reyn.tools.action_index`)
│   ├── registry-cache/     mcp registry cache
│   └── budget_checkpoint.json  compacted per-agent budget totals (#2945),
│                           anchored to a byte position in
│                           `state/budget_ledger.jsonl` — fully
│                           reconstructable from the ledger by re-scanning,
│                           safe to delete at any time (a write failure is
│                           logged and swallowed, never blocks startup).
│                           NOTE: its per-agent totals act as a FLOOR by
│                           default — whenever the ledger is found
│                           truncated, missing, or its identity (a hash of
│                           the ledger's leading line, #3201) cannot be
│                           established on one or both sides. The ONE
│                           exception is a ledger AFFIRMATIVELY proven, by
│                           that identity hash, to be a genuinely DIFFERENT
│                           ledger — that gets no floor. Only an explicit
│                           operator action (archiving/deleting BOTH this
│                           file and the ledger together) resets per-agent
│                           spend otherwise; `/budget` surfaces the floor
│                           fact + reason whenever it fires. See
│                           reference/config/budget.md
└── topologies/             RECOVERY-CORE — agent topologies (reconstructed from topology_* WAL)
```

`reyn.yaml`, `secrets.env`, `oauth_tokens.json`, and `capability_profiles/` are
**operator/user-owned** and live under the project root / `.reyn/` but are **outside**
Reyn's time-travel — they are never captured or reverted.

### Move map (clean break, no migration)

The reorg has **no backward-compat shim**: old top-level paths simply stop being
read/written. Anyone with an older `.reyn/` (config files directly under `.reyn/`, caches
mixed in at the top level) should know things moved:

| Was | Now |
|---|---|
| `.reyn/mcp.yaml`, `.reyn/cron.yaml`, `.reyn/hooks.yaml`, `.reyn/integrations.yaml` | `.reyn/config/<x>.yaml` |
| `.reyn/index/sources.yaml` | `.reyn/config/index/sources.yaml` |
| `.reyn/index/` (data), `.reyn/action_index/`, `.reyn/registry-cache/` | `.reyn/cache/…` |
| `.reyn/approvals.yaml` | **unchanged** (top-level — it is *persist*, not recovery-core config) |

## <a id="recovery-core"></a>Recovery-core: what the WAL + snapshot generators write

Recovery-core has two tiers — **authoritative** and **derived** — both reconstructed by the
same rewind path (WAL replay + snapshot generations — see
[Time travel](../../concepts/runtime/time-travel.md)):

- **Authoritative** (the recovery TRUTH — write-gated):
  - `.reyn/state/wal.jsonl` — the append-only, seq'd WAL: the complete WAL-event history
    everything else is replayed from. Also `.reyn/state/budget_ledger.jsonl`.
  - `.reyn/config/<x>.yaml` — the agent-edited config registries. Each mutation goes
    through a dedicated op that writes a **full-state config generation** (seq-keyed,
    truncation-surviving); the `.yaml` is materialised from the generation at the target
    seq on rewind.
  - `.reyn/state/agent_identity/<name>@<seq>.json` — per-agent identity + frozen spawn
    lineage, recorded as a **full-state generation** (seq-keyed, truncation-surviving).
    The WAL event is dropped below the truncation floor, so rewind reconstructs the
    ⊆-parent cap from the generation — without it a long-lived agent's child runs
    un-capped on rewind.
- **Derived** (reconstructable from the authoritative state — NOT write-gated):
  - `.reyn/agents/<name>/state/`: `snapshot.json`, `generations/gen-<seq>.json`,
    `sessions/<sid>/…`. Runtime snapshots are seq-keyed generations reconstructable from
    WAL replay (fall back to an earlier generation, or replay from genesis). A corrupted
    snapshot is *recoverable*, not data loss — the same reconstructability logic as
    `cache/`. Agent-identity and lineage are likewise stored as seq-keyed generation
    snapshots (truncation-surviving, same generation-store pattern as config). (This is
    why the write-gate, below, covers only the authoritative tier.)

**Not recovery-core:** `.reyn/state/run_registry.json` — A2A's own async-run store
(`RunRegistry`). It is a standalone, atomically-written (tmp → `Path.replace()`)
full-state snapshot, independent of the WAL, so it trivially survives WAL
truncation — but it does not participate in rewind: A2A/web is a process
singleton (see [A2A concepts](../../concepts/multi-agent/a2a.md)), and an
external A2A run is durable + query-coherent but intentionally does not
time-travel with a session's own rewind.

## The recovery-core write-gate (the rule you hit as a workflow author)

**A raw `file.write` to `.reyn/config/` or `.reyn/state/` is DENIED.** The
**authoritative** recovery-core (the WAL at `.reyn/state/` + the `.reyn/config/` registries —
see [above](#recovery-core)) must be mutated through a **dedicated op** — never a generic
`file.write` — so the change lands in the recovery stream (WAL entry or config generation)
and can be reconstructed or reverted on rewind. The directory boundary *is* the write-gate
boundary. (The *derived* per-agent snapshots under `.reyn/agents/<name>/state/` are
reconstructable from the WAL, so they are not write-gated — a corrupted snapshot is
recoverable, not data loss.)

To change config, call the dedicated op (which writes the `.yaml` as a **new config generation**):

- MCP servers → `mcp_install` / `mcp_drop_server`
- cron → `cron_register` / `cron_unregister` / `cron_enable`
- hooks → `hooks_add`
- index sources → the index ops

`approvals.yaml` (top-level *persist*) is likewise write-gated — it is written only via the
permission-approval flow (`_persist`), never a raw `file.write`. `memory/`, `cache/`, and
other non-recovery-core `.reyn/` paths are ordinary writable zones.

## Where does a new subsystem put its data?

Ask the two recovery-core questions:

1. **Is it run-authored AND does it affect in-memory runtime state that recovery
   reconstructs?** → **recovery-core**: put it under `state/` (and write it through a
   WAL-emitting durable path or a dedicated op — never a raw `file.write`). If it's a
   config-style registry the agent mutates, put it under `config/` and give it a dedicated
   op that writes a config generation (full-state, seq-keyed).
2. Otherwise pick the exclusion that fits:
   - rebuildable from other state → `cache/`
   - a write-only forensic record → `events/` (or a sibling audit dir)
   - knowledge / a decision that must **survive** rewind → `memory/` (persist)
   - operator/user-owned → it does not belong under Reyn's managed tree.

When in doubt, do **not** default to recovery-core — an over-broad recovery-core entry
either bloats capture or, if it can't be reconstructed, breaks rewind.

## See also

- [Time travel](../../concepts/runtime/time-travel.md) — rewind/fork/PITR mechanics.
- [State directory](../config/state-dir.md) — `--state-dir` routing.
