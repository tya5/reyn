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
| **persist** (knowledge / decisions) | survives rewind — never reverted | `memory/`, `approvals.jsonl` |
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
│   ├── composer_pending.json  armed `durable` Composer state — standalone snapshot, NOT recovery-core (see below)
│   └── sessions/<sid>/     per-spawned-session snapshot + generations
│                           (each also carries its own composer_pending.json)
├── config/                 RECOVERY-CORE — agent-managed registries (reconstructed by replay)
│   ├── mcp.yaml            MCP servers   (mcp_install / mcp_drop_server)
│   ├── cron.yaml           cron jobs     (cron_register / …)
│   ├── hooks.yaml          push hooks    (hooks_add)
│   ├── skills.yaml         skill install/registration entries (skill_install, #2548 PR-B)
│   ├── pipelines.yaml      pipeline install/registration entries (pipeline_install)
│   ├── presentations.yaml  presentation-template entries (presentation_install, FP-0054 PR-C)
│   ├── index/sources.yaml  index source manifest (index ops)
│   └── integrations.yaml   ⏳ designed, not yet wired — no reader/writer exists (#4337);
│                           `external_transports` config is read from reyn.yaml's own
│                           `external_transports:` section only
├── memory/                 PERSIST — agent knowledge; survives rewind, never reverted
│   ├── artifact_refs.jsonl (agent, path) → ref table for `present`'s "artifact"
│   │                       component (#4482 PR-1, moved from `cache/` by #4584):
│   │                       the mapping exists ONLY at mint time — no WAL event,
│   │                       no conversation-log entry, nothing else durable
│   │                       carries it — so it is PRIMARY data, not derived,
│   │                       however small. Ordinary writable zone (not
│   │                       write-gated, same as the rest of `memory/`) — a
│   │                       plain file write, never a dedicated op. "memory" is
│   │                       an imperfect NAME fit (a ref→path table is neither
│   │                       knowledge nor a decision) — the doc's own tier
│   │                       decision rule (below) only points at one place.
│   └── tool_result_spills.jsonl  persisted provenance of history-content SPILL
│                           artifacts under `tool-results/` (#4381/#4432, moved
│                           from `cache/` by #4584 — same reasoning as
│                           `artifact_refs.jsonl` above: not literally
│                           rebuildable, an earlier comment on this file said
│                           so explicitly while still filing it under
│                           `cache/`) — one JSON line per `MediaStore.
│                           save_tool_result` write, read back at every
│                           `MediaStore` construction so a bare re-read of a
│                           spilled file (in this process or a later one — a
│                           reference can outlive the process that wrote it,
│                           sitting in `history.jsonl`) is detected and errors
│                           instead of re-spilling. SELF-PRUNES existing
│                           entries whose target vanished (bounds otherwise-
│                           unbounded growth) but does NOT rebuild if the
│                           manifest FILE ITSELF is deleted — pruning and
│                           rebuilding are not the same claim.
│   └── history-content/<agent>/<session_id>/  PERSIST (#5364) — full
│                           tool-result bodies `history.jsonl` references,
│                           GB-CLASS by design (a single oversized turn is
│                           the whole point this store exists to absorb —
│                           #5364, owner: "普通に考えれば最新にでかい
│                           もの置くだけで会話継続できないとわかるで
│                           しょ"). One directory per (agent, session) pair
│                           — the SESSION-ID-ONLY shape #5369 originally
│                           shipped put every agent's default `main`
│                           session under ONE shared directory (#5383's
│                           own key-space fix; `history_content_root`
│                           itself is unchanged, so an already-minted
│                           flat ref still resolves — no migration
│                           needed). `MediaStore.
│                           save_tool_result`'s current write target (the
│                           pre-#5364 `tool-results/` AUDIT dir below is
│                           frozen: read-only going forward, no migration).
│                           NESTED deliberately — 4 separate `memory/`
│                           scanners (`reyn.data.memory.memory.
│                           list_entries`, `knowledge_ingest.
│                           _iter_memory_entries`, `tools.memory.
│                           _regenerate_index`, `op_runtime.file.
│                           regenerate_index_impl`) each do a non-recursive
│                           `glob("*.md")` over `memory/`'s own direct
│                           children — a flat `.md` here would make all
│                           four read this store's entire footprint
│                           (pinned by `tests/data/test_5364_history_
│                           content_nesting.py`, one test per scanner).
│                           ⏳ #5364's own history-resolution/GC follow-up
│                           work (a pure `inline`/`ref`/`lost` resolver;
│                           the permanent-write-failure fallback; the
│                           directory size-cap GC) is designed but not yet
│                           implemented as of this entry — see #5364 for
│                           current status before assuming any of it
│                           exists.
├── approvals.jsonl         PERSIST — user-authored permission grants, append-only ledger
│                           (#5153); survive rewind. approvals.yaml (legacy snapshot) is
│                           migrated into this once, on first touch, then inert history.
├── events/ traces/ logs/   AUDIT — append-only forensic record; never restored
│   audit-trail/ tool-results/ media/
│                           `media/` (#4478): flat `<file>` for a legacy
│                           write with no agent identity, `<agent>/
│                           <session_id>/<file>` for a real one (the
│                           SAME nesting shape `history-content/` above
│                           already uses, `MediaStore.media_content_dir_
│                           for`) — counts toward the SAME project-wide
│                           `storage.max_bytes`/`storage.pin` cap as
│                           `history-content/`, not a separate one; a
│                           flat (legacy) file is a pin-unprotected
│                           eviction candidate, disclosed, never
│                           migrated.
├── cache/                  DERIVED — rebuilt after restore. ⚠️ "rebuilt after
│                           restore" describes what belongs in this tier, not a
│                           blanket safety claim about deleting `cache/` itself:
│                           `index_drop` (an agent-callable op) deletes
│                           `cache/index/<source>` behind `require_file_write`
│                           (#1199 S3.4) — reyn's OWN code already treats
│                           deleting something under `cache/` as a gated,
│                           permission-requiring action, not a free one.
│   ├── index/              rag index data (sqlite), one dir per source —
│   │   └── actions/        includes the tool-use action catalog since
│   │                       FP-0057 Phase 0 (was the separate action_index/
│   │                       implementation pre-consolidation; clean-break,
│   │                       no migration — see `reyn.tools.action_index`)
│   ├── registry-cache/     mcp registry cache
│   └── budget_checkpoint.json  compacted per-agent budget totals (#2945),
│                           anchored to a byte position in
│                           `state/budget_ledger.jsonl` — fully
│                           reconstructable from the ledger by re-scanning.
│                           UNLIKE the rest of `cache/` (see the ⚠️ above),
│                           THIS one entry really is safe to delete at any
│                           time (a write failure is
│                           logged and swallowed, never blocks startup) —
│                           scoped to this file specifically, not a claim
│                           about `cache/` as a whole.
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
| `.reyn/mcp.yaml`, `.reyn/cron.yaml`, `.reyn/hooks.yaml` | `.reyn/config/<x>.yaml` |
| `.reyn/index/sources.yaml` | `.reyn/config/index/sources.yaml` |
| `.reyn/index/` (data), `.reyn/action_index/`, `.reyn/registry-cache/` | `.reyn/cache/…` |
| `.reyn/approvals.yaml` | `.reyn/approvals.jsonl` (#5153 — still top-level *persist*, not recovery-core config; `approvals.yaml` migrates in once, then is inert) |

`integrations.yaml` is deliberately absent from this table (#4337): no reader/writer for it
exists at either the old or the new location, so there is nothing that actually moved — see
the `config/` entry above.

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

**Not recovery-core:** `<per-session state dir>/composer_pending.json` — the armed
pending set of every `durable` Composer (`op: deadline` by default, see
[reyn-yaml § `composers`](../config/reyn-yaml.md#composers-block)). Same shape and
same reasoning as `run_registry.json`: a standalone, atomically-written full-state
snapshot that is never derived from the WAL, so it survives truncation structurally
rather than by argument — the resolution #2259 established for config recovery. It is
per-SESSION (not `.reyn/state/`) because composers are per-session; two sessions of the
same agent arming the same composer name must not overwrite each other. It does not
participate in rewind: an armed dead-man switch is a live monitor of wall-clock time,
not a piece of the session's reconstructable history.

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

`approvals.jsonl` (top-level *persist*) is likewise write-gated for its primary writer — the
security-side permission-approval flow (`_persist`) never does a raw `file.write`. **#5153**
moved every writer (`_persist`, the CLI's `reyn permissions revoke`/`clear`, and the
`/api/permissions` REST router's `revoke_permission`/`clear_permissions`,
`interfaces/web/routers/permissions.py`) onto the SAME `ApprovalLedger.append_approval`
primitive — an append-only, fsync'd-per-line record, never a snapshot read-modify-write —
closing what used to be a second, independent `_save` (`path.write_text`) writer with its own
race exposure. What it must not do is append silently: each REST route above still emits its
own audit-event (`permission_approval_revoked` / `permission_approvals_cleared`, #5065)
alongside the append, so `approvals.jsonl`'s permission-shape changes stay observable through
`.reyn/events` regardless of which caller made them. `memory/`, `cache/`, and
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
