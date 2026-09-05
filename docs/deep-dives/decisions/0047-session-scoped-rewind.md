# ADR-0047 (#5769) — rewind gains a session scope; the global cut becomes the `scope=None` case

**Status**: **PROPOSED** (owner ruling 2026-09-05, verbatim 「両方YES」 to the two questions in [#5769](https://github.com/tya5/reyn/issues/5769); to be raised to ACCEPTED only after the implementing PR lands and each decision below is checked against `origin/main`, the same procedure ADR-0045 followed).

**Supersedes**: [ADR-0038](0038-user-facing-time-travel-rewind.md) — **its rejected alternative "Per-agent scoped rewind" only**. D2's global consistent cut stays as the **default** and as one of the two shapes this ADR defines; nothing else in 0038 (D3's append-only reset-record, D8's checkout, the retention guard) changes.

**Track**: #5769.

## Context

ADR-0038 D2 made rewind a global consistent cut and, in its Alternatives table, rejected per-agent scoped rewind with this reasoning (verbatim):

> requires splitting the workspace + WAL per agent, which collides head-on with the single-SSoT / global-single-seq architecture (D2).

The owner asked for a session-local rewind: rewinding one `(agent, session)` pair while every other session stays at head.

Measured against `origin/main` on 2026-09-05, the rejection's premise no longer describes the code:

- The WAL is still one global single-seq log, but **every entry already routes to exactly one `(agent_name, session_id)`** (`agent_snapshot.py`'s `event_route_key`; legacy entries default to `"main"`). Scoping which entries are *active* does not require splitting the log.
- Snapshot generations, `reconstruct`, and `_materialize_rewind` are **already per-`(agent, sid)`** — `_materialize_rewind` iterates `for name … for sid in _discover_session_ids(name)`.
- The shared workspace's file content is **not reverted by the global rewind today** (no such mechanism exists in `registry.py` or `core/`); `workspace_at_or_below` is the agent/session lifecycle drop-cut, not file content. So a session-scoped rewind that leaves the workspace alone is not a regression from what the global cut does.

The only globally-derived thing in the substrate is `is_active(seq)`: the reset-record carries `(R, target_n)` with **no scope field**, and `_rewind_records` reads every rewind record regardless of agent.

## The decision

1. **A reset-record carries a scope.** `(R, target_n, scope)` where `scope` is `(agent_name, session_id)` or `None`. **`None` means global** — exactly today's record. A legacy record with no field reads as `None`, so existing WALs need no migration and existing behaviour is unchanged.
2. **`is_active` is derived per scope, and the derivation belongs where the scope is known.** For a given `(agent, sid)`, the rewind chain is the global records **plus** the records scoped to that pair; `_abandoned_intervals`' latest-first composition is reused unchanged over that set. `build_active_predicate(state_log, *, scope)` takes the scope as a **required keyword-only argument** — never defaulted, so a consumer that forgets it fails at the call rather than silently behaving globally.

   **Revised 2026-09-05 (#5769 stage 2, after stage 1 landed in [#5772](https://github.com/tya5/reyn/pull/5772)).** The first form of this decision said only that the predicate takes a scope; it left the predicate itself as a value a caller could build once and pass around. That is not sufficient, and the reason is the decision:

   > **Scope is a property of the seq, not of the predicate object.** Every WAL entry routes to exactly one `(agent_name, session_id)` (`event_route_key`), so the correct rewind chain for a seq is its OWNER's chain. A predicate built once and applied to seqs of several owners answers all of them from one owner's chain, and nothing in its type or name says which.

   Stage 1 measured the shape this produces: at six call sites the predicate was hoisted **above the very loop whose variables are the scope** (`_materialize_rewind`: `is_active` built above `for name … for sid …`, then asked about `sess_seq`, whose owner is `(name, sid)`). Those sites read as "global" for a reason — the hoist — that is itself what stage 2 changes. A discriminant asked about the call's *aggregate* shape therefore passes exactly the sites that must move.

   **Therefore: the derivation is built where the scope is known, and a consumer passes a scope, never a predicate.** Concretely — a collaborator that used to accept a caller-hoisted `is_active` (`ConfigGenerationStore.latest_active`, `PipelineStateStore.latest_active`) takes `(state_log, *, scope)` and builds its own; a loop over owners builds inside the loop. The mis-scoped-predicate failure mode is then **unwritable**, not merely discouraged — the shape this ADR prefers over a rule in a docstring.

   **The performance premise the old shape rested on is gone.** Those call sites carry `#2941` comments requiring one hoisted predicate "so the WAL is not re-scanned per X". Since **#2939** the rewind-record fetch is an incremental, per-`StateLog` cached tail-reader (`_RewindIndex`), so a fresh `build_active_predicate` costs one fold over the rewind records — O(records), not O(WAL). Those comments describe a world that no longer exists and are rewritten with this change. (A structure adopted for a performance reason outlives the reason; the reason must be re-read, not inherited.)

3. **`checkout(seq, *, scope)`.** `scope=None` is the existing five-step global cut, untouched. A scoped checkout applies the same retention guard (the WAL floor is global and stays so), cancels and quiesces **only** the target session, appends one scoped reset-record (fsync'd before any reconstruction — the crash-idempotence keystone from 0038 is unchanged), and materialises **only** that `(name, sid)`.
4. **A session-scoped rewind rewinds conversation and agent state only.** It never touches the shared workspace's files (neither does the global cut today) and it does not touch config generations (`config_generations.py` has no session notion; `mcp.yaml` / `cron.yaml` are workspace-level SSoT, outside a session's scope by construction).
5. **Cross-session consistency is deliberately given up by a scoped rewind.** An A→B message is B's WAL entry (routed by `target`). Rewinding A alone leaves B holding a message A no longer remembers sending. This is the feature's definition, accepted by the owner, not a defect. **A caller who needs the consistent cut uses `scope=None`.**

6. **Agent-level lifecycle is global; only session-level state is scoped.** A session-scoped rewind never creates, drops, archives, purges or un-archives an **agent** — those facts are agent-wide (`agent_created` / `agent_archived` / `agent_purged` carry `entity_kind="agent", name=…` and no session; the `.archived` tombstone is one marker per agent directory; archiving preserves every session, #1954). One session rewinding must not change the workspace's topology for every other session. What a scoped rewind does move is session-level state: session create / vanish, and the conversation and agent state of that `(name, sid)`. In code this is the split `_materialize_rewind` now makes — `GLOBAL_SCOPE` for the agent existence checks, `scope=(name, sid)` built inside the session loop. **This resolves `_reconcile_archived_as_of_cut`, which stage 2 left on hold**: archival is agent-wide, so `GLOBAL_SCOPE` is its final answer, not a placeholder. Decision 4 is the same rule applied to config generations, which have no session notion at all.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the global cut only (0038 as is) | The owner asked for session-local rewind; the rejection's premise ("requires splitting workspace + WAL") is not what the code does. |
| Split the WAL per session | Unnecessary — entries are already session-routed on the one log, and a split would break the single seq axis that cross-agent ordering rides on. |
| `scope=None` as a default argument on the predicate | Rejected: with 9 consumers (the count measured on `origin/main` during #5772 co-vet; an earlier "12" counted docstring mentions), one forgotten call site silently behaves globally and session-local rewind stops working for exactly that consumer, with no red. Required keyword-only fails loudly instead. |
| The caller hoists one predicate and passes it to collaborators (stage 1's shape) | Rejected on measurement: it lets a predicate built for one owner be asked about another owner's seq, and the hoist is invisible at the place the wrong answer is produced. Passing a **scope** instead makes the mis-scoped question unwritable. See decision 2's revision. |
| Revert workspace files for a scoped rewind | Impossible without per-session workspaces, which 0038 rejected for destroying the single-SSoT invariant — and the global cut does not revert files today either. |

## Consequences

**Desirable**
- Global rewind is unchanged by construction (`scope=None` is the same record and the same chain).
- No new persisted structure beyond one optional field on an existing record kind; legacy WALs read as before.
- The recovery keystone (fsync the reset-record, then materialise) is reused; crash mid-scoped-rewind recovers by reading the scope off the latest reset-record.

**Undesirable, and stated**
- Sessions can diverge (decision 5). The product surface must say which shape a `/rewind` is.
- `list_rewind_points` today enumerates only each agent's main-session store; a per-session picker must iterate `_discover_session_ids` and tag rows with `sid` (the loop shape `_materialize_rewind` already uses).
- `recover_rewind_if_needed` reads one global `active_rewind_target`; it must read `(target_n, scope)` and materialise only the scoped pair when the latest record is scoped.

## Acceptance (for raising to ACCEPTED)

- [ ] `scope=None` checkout leaves every existing rewind test green and unmodified.
- [ ] `checkout(N, scope=(A, s))` puts `A/s` as-of-N and leaves every other session at head.
- [ ] A WAL containing an A→B message, A rewound alone: A forgets sending, B keeps receiving — pinned as the intended property.
- [ ] A legacy reset-record (no scope field) reads as global.
- [ ] Calling `build_active_predicate` without `scope` fails.
- [ ] Crash mid-scoped-rewind, then recover: only the scoped session is as-of-N (truncate-falsify form).
- [ ] A target below the global WAL floor is rejected for a scoped checkout too.
- [ ] No collaborator takes a caller-built `is_active` predicate: `git grep -nE 'is_active: .*Callable' -- src/` is empty.
- [ ] A predicate is never built above a loop whose variables are the scope (`_materialize_rewind`'s session predicate is built inside the `sid` loop).
- [ ] A session-scoped rewind leaves every agent's existence and `.archived` state untouched (decision 6).
- [ ] ADR-0038 is byte-identical.

## References

- [#5769](https://github.com/tya5/reyn/issues/5769) — feasibility measurement and design.
- [ADR-0038](0038-user-facing-time-travel-rewind.md) D2 / D3 / D8 — the substrate this extends.
- `src/reyn/core/events/snapshot_generations.py` (`_rewind_records`, `_abandoned_intervals`, `build_active_predicate`, `checkout`), `src/reyn/runtime/registry.py` (`checkout`, `_materialize_rewind`, `recover_rewind_if_needed`, `list_rewind_points`), `src/reyn/core/events/agent_snapshot.py` (`event_route_key`).
