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

   **The default the USER sees is session-local (owner ruling 2026-09-05).** Asked which of four shapes `/rewind` should take now that it means two different operations, the owner answered — verbatim, relayed by lead-coder from telegram, 2026-09-05 08:47 — 「**規定はローカルがよいな**」. So: **`/rewind` with no further words rewinds only the current `(agent, session)`; the global consistent cut is chosen explicitly.** This **is** a behaviour change and was put to the owner as one: `/rewind` is global today.

   ⚠️ **Two layers, two different answers, both deliberate.** The **API** has no default — `build_active_predicate(state_log, *, scope)` and `checkout(…, *, scope)` take a required keyword (decision 2), so no caller can omit it and drift to global. The **UI** does have a default, and it is session-local. A future reader finding "required, never defaulted" next to "the default is local" is not reading a contradiction: the command layer is the one place that decides what the user meant, and it then states that scope explicitly to an API that refuses to guess.
4. **A session-scoped rewind rewinds conversation and agent state only.** It never touches the shared workspace's files (neither does the global cut today) and it does not touch config generations (`config_generations.py` has no session notion; `mcp.yaml` / `cron.yaml` are workspace-level SSoT, outside a session's scope by construction).
5. **Cross-session consistency is deliberately given up by a scoped rewind.** An A→B message is B's WAL entry (routed by `target`). Rewinding A alone leaves B holding a message A no longer remembers sending. This is the feature's definition, accepted by the owner, not a defect. **A caller who needs the consistent cut uses `scope=None`.**

6. **Agent-level lifecycle is global; only session-level state is scoped.** A session-scoped rewind never creates, drops, archives, purges or un-archives an **agent** — those facts are agent-wide (`agent_created` / `agent_archived` / `agent_purged` carry `entity_kind="agent", name=…` and no session; the `.archived` tombstone is one marker per agent directory; archiving preserves every session, #1954). One session rewinding must not change the workspace's topology for every other session. What a scoped rewind does move is session-level state: session create / vanish, and the conversation and agent state of that `(name, sid)`. In code this is the split `_materialize_rewind` now makes — `GLOBAL_SCOPE` for the agent existence checks, `scope=(name, sid)` built inside the session loop. **This resolves `_reconcile_archived_as_of_cut`, which stage 2 left on hold**: archival is agent-wide, so `GLOBAL_SCOPE` is its final answer, not a placeholder. Decision 4 is the same rule applied to config generations, which have no session notion at all.

7. **A consumer that cannot name a seq's owner must not answer from the global chain.** `GLOBAL_SCOPE` does not mean "apply every reset-record"; it means "apply only the UNSCOPED ones" (decision 2's filter). So a consumer that falls back to `GLOBAL_SCOPE` because it failed to determine the owner **ignores every scoped rewind** — it retains, or re-wakes, exactly the state the user rewound away, silently. Measured instance: `_rewake_pipeline_runs` derives its scope from `invocation.json`; a run whose `invocation.json` is unreadable has no nameable owner. **The rule: skip the item and make the skip observable, rather than defaulting to global.** An un-rewoken pipeline run is parked, visible and recoverable; a re-woken one spends budget on work the user discarded, and cost/budget bounding is a band member. This fires only once a writer for a non-`None` scope exists — stage 3 — but it is a decision, not an implementation detail, because the alternative reads as a harmless default at every site that takes it.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the global cut only (0038 as is) | The owner asked for session-local rewind; the rejection's premise ("requires splitting workspace + WAL") is not what the code does. |
| Split the WAL per session | Unnecessary — entries are already session-routed on the one log, and a split would break the single seq axis that cross-agent ordering rides on. |
| `scope=None` as a default argument on the predicate | Rejected: with 9 consumers (the count measured on `origin/main` during #5772 co-vet; an earlier "12" counted docstring mentions), one forgotten call site silently behaves globally and session-local rewind stops working for exactly that consumer, with no red. Required keyword-only fails loudly instead. |
| The caller hoists one predicate and passes it to collaborators (stage 1's shape) | Rejected on measurement: it lets a predicate built for one owner be asked about another owner's seq, and the hoist is invisible at the place the wrong answer is produced. Passing a **scope** instead makes the mis-scoped question unwritable. See decision 2's revision. |
| **UI: no default at all — `/rewind` refuses to run until the user names the target** (architect and lead-coder both recommended this; the shape decision 2 takes one layer down) | **Not chosen by the owner**, who ruled the default is session-local. Recorded because the recommendation was made and declined, not because it is still open. |
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
- **The branch/fork display changes shape, and that is UX-visible.** Decision 2 applies to every reader of the reset-record chain, not only the ones a rewind writes through: `branch_ids_for`, `list_branches` and `lineage_predecessor` derive what the fork UX draws, and a scope-blind derivation lets one session's scoped rewind put an abandonment on *every* session's checkpoints. Making them scope-aware is required for correctness (#5789) and **changes which branches a user sees and how they group** — so the resulting screen is owner-gated, per the standing rule that only UX-visible changes need the owner. The correctness fix is not owner-gated; the presentation is.
- **A reader that is merely "not narrowed yet" is indistinguishable from one that is deliberately global.** This ADR's first two stages left that distinction in prose ("a later stage's own decision, not made here") attached to a shared helper, and it was read by eight call sites at once. When the writer landed, the decision came due at all eight and was taken at none, and one of them (`reconstruct`) reached `main` as a silent defect — a scoped rewind hiding another session's own WAL entries, no exception, just a shorter history. **A pending decision needs a subject who is told it came due**; prose that names a future stage does not notify that stage. #5789 closes this by deleting the scope-blind accessor so the remaining readers cannot avoid stating a scope.

## Acceptance (for raising to ACCEPTED)

Checked against `origin/main` on **2026-09-05**, after #5772 (stage 1) and #5775 (stage 2) merged. **Decisions 3, 5 and 7 have no implementation yet — stage 3 carries the writer — so this ADR stays PROPOSED.** The unchecked boxes below are the reason, not an oversight.

- [x] `scope=None` checkout leaves every existing rewind test green and unmodified.
- [ ] `checkout(N, scope=(A, s))` puts `A/s` as-of-N and leaves every other session at head. — **stage 3**: `checkout(state_log, *, target_seq, supersedes)` and `AgentRegistry.checkout(seq)` on `origin/main` take no `scope`.
- [ ] A WAL containing an A→B message, A rewound alone: A forgets sending, B keeps receiving — pinned as the intended property. — **stage 3** (needs the writer).
- [x] A legacy reset-record (no scope field) reads as global.
- [x] Calling `build_active_predicate` without `scope` fails.
- [ ] Crash mid-scoped-rewind, then recover: only the scoped session is as-of-N (truncate-falsify form). — **stage 3**.
- [ ] A target below the global WAL floor is rejected for a scoped checkout too. — **stage 3**.
- [x] No collaborator takes a caller-built `is_active` predicate. Census on `origin/main`: `git grep -nE 'is_active: .*Callable' -- src/` returns **one** hit, `Session._filter_visible_on_active_branch` — a private static helper shared by two methods that each build their own predicate first, not a cross-owner seam. Disclosed, not empty.
- [x] A predicate is never built above a loop whose variables are the scope (`_materialize_rewind`'s session predicate is built inside the `sid` loop).
- [ ] A session-scoped rewind leaves every agent's existence and `.archived` state untouched (decision 6). — **stage 3**: the rule is implemented (`GLOBAL_SCOPE` on the agent-level checks), but nothing can perform a session-scoped rewind yet to witness it.
- [ ] An item whose owner cannot be named is skipped observably, not answered globally (decision 7). — **stage 3**.
- [x] ADR-0038 is byte-identical.

## References

- [#5769](https://github.com/tya5/reyn/issues/5769) — feasibility measurement and design.
- [ADR-0038](0038-user-facing-time-travel-rewind.md) D2 / D3 / D8 — the substrate this extends.
- `src/reyn/core/events/snapshot_generations.py` (`_rewind_records`, `_abandoned_intervals`, `build_active_predicate`, `checkout`), `src/reyn/runtime/registry.py` (`checkout`, `_materialize_rewind`, `recover_rewind_if_needed`, `list_rewind_points`), `src/reyn/core/events/agent_snapshot.py` (`event_route_key`).
