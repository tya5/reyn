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
2. **`is_active` is derived per scope.** For a given `(agent, sid)`, the rewind chain is the global records **plus** the records scoped to that pair; `_abandoned_intervals`' latest-first composition is reused unchanged over that set. `build_active_predicate(state_log, *, scope)` takes the scope as a **required keyword-only argument** — never defaulted, so a consumer that forgets it fails at the call rather than silently behaving globally. Every existing consumer already knows which `(agent, sid)` it is evaluating; it passes a fact it holds, it does not compute a decision.
3. **`checkout(seq, *, scope)`.** `scope=None` is the existing five-step global cut, untouched. A scoped checkout applies the same retention guard (the WAL floor is global and stays so), cancels and quiesces **only** the target session, appends one scoped reset-record (fsync'd before any reconstruction — the crash-idempotence keystone from 0038 is unchanged), and materialises **only** that `(name, sid)`.
4. **A session-scoped rewind rewinds conversation and agent state only.** It never touches the shared workspace's files (neither does the global cut today) and it does not touch config generations (`config_generations.py` has no session notion; `mcp.yaml` / `cron.yaml` are workspace-level SSoT, outside a session's scope by construction).
5. **Cross-session consistency is deliberately given up by a scoped rewind.** An A→B message is B's WAL entry (routed by `target`). Rewinding A alone leaves B holding a message A no longer remembers sending. This is the feature's definition, accepted by the owner, not a defect. **A caller who needs the consistent cut uses `scope=None`.**

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the global cut only (0038 as is) | The owner asked for session-local rewind; the rejection's premise ("requires splitting workspace + WAL") is not what the code does. |
| Split the WAL per session | Unnecessary — entries are already session-routed on the one log, and a split would break the single seq axis that cross-agent ordering rides on. |
| `scope=None` as a default argument on the predicate | Rejected: with 12 consumers, one forgotten call site silently behaves globally and session-local rewind stops working for exactly that consumer, with no red. Required keyword-only fails loudly instead. |
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
- [ ] ADR-0038 is byte-identical.

## References

- [#5769](https://github.com/tya5/reyn/issues/5769) — feasibility measurement and design.
- [ADR-0038](0038-user-facing-time-travel-rewind.md) D2 / D3 / D8 — the substrate this extends.
- `src/reyn/core/events/snapshot_generations.py` (`_rewind_records`, `_abandoned_intervals`, `build_active_predicate`, `checkout`), `src/reyn/runtime/registry.py` (`checkout`, `_materialize_rewind`, `recover_rewind_if_needed`, `list_rewind_points`), `src/reyn/core/events/agent_snapshot.py` (`event_route_key`).
