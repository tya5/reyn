"""SnapshotJournal — owns AgentSnapshot + StateLog WAL (extracted from Session wave 1).

All WAL-recorded mutations go through here; in-memory readers go via the
`.snapshot` property.
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from pathlib import Path

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog


class SnapshotJournal:
    """Owns AgentSnapshot + StateLog WAL.

    All WAL-recorded mutations go through here; in-memory readers go via
    the `.snapshot` property.

    Parameters
    ----------
    agent_name:
        Name of the agent this journal belongs to.
    snapshot_path:
        Path where the snapshot JSON is persisted (atomic write).
    state_log:
        Process-shared WAL instance.  When ``None``, persistence is
        disabled (tests / non-chat invocations) — all WAL operations
        become no-ops but in-memory state is still maintained where
        relevant.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        snapshot_path: Path,
        state_log: StateLog | None,
        generation_store: SnapshotGenerationStore | None = None,
        session_id: str = "main",
    ) -> None:
        self._agent_name = agent_name
        # FP-0043 Stage 5: the conversation session this journal records for. Tagged
        # onto every WAL append (session_id=) so replay routes entries to the right
        # per-session AgentSnapshot; default "main" = byte-identical single session.
        # Set post-construction by spawn_session for spawned sessions (set_session_id)
        # — mirroring the _anchor_store post-construction pattern.
        self._session_id = session_id
        self._snapshot_path = Path(snapshot_path)
        self._state_log = state_log
        # ADR-0038 Stage 1a: PITR generation store. When None (tests /
        # non-chat), generation cuts are no-ops and the single snapshot.json
        # path is unchanged (no behavior change).
        self._generation_store = generation_store
        # #1547: per-checkpoint anchor text (truncated last user message). Set
        # post-construction by the registry. None → no anchor capture.
        self._anchor_store = None
        self._snapshot: AgentSnapshot = AgentSnapshot.empty(agent_name, session_id)

    async def _wal_append(self, kind: str, **fields):
        """FP-0043 Stage 5: the single WAL-append chokepoint for this journal.

        Injects ``session_id=self._session_id`` into EVERY entry so replay routes
        it to this session's snapshot (the funnel-completeness guarantee — a future
        journal append inherits session-tagging by construction). Preserves the
        prior no-state_log behaviour (returns None when the journal has no WAL)."""
        if self._state_log is None:
            return None
        log = self._state_log  # local so the funnel replace doesn't recurse into this call
        return await log.append(kind, session_id=self._session_id, **fields)

    def _wal_append_nowait(self, kind: str, **fields) -> None:
        """#2259 PR-2b: the NON-BLOCKING WAL-append chokepoint — fire-and-forgets the durable write
        through the worker. Same session-tagging funnel as `_wal_append`. Returns nothing: the seq
        is assigned IN the worker's WAL job (seq-in-worker — never synchronously on the loop, so no
        durable artifact can reference a not-yet-durable seq). The paired `save_nowait` reads
        `state_log.last_assigned_seq` (the seq this WAL job assigned) to stamp the snapshot. Pairs
        with `save_nowait` — called back-to-back with NO await between, so the (WAL, snapshot) enqueue
        is atomic on the loop (invariant #2). No-op without a WAL."""
        if self._state_log is None:
            return
        log = self._state_log
        log.append_nowait(kind, session_id=self._session_id, **fields)

    def set_session_id(self, session_id: str) -> None:
        """FP-0043 Stage 5: set the conversation session id post-construction
        (spawn_session uses this for a spawned session, before its run-loop goes
        live — mirroring set_anchor_store). The in-memory snapshot's session_id
        is updated too so its save() + apply routing stay consistent."""
        self._session_id = session_id
        self._snapshot.session_id = session_id

    def set_snapshot_path(self, snapshot_path: Path) -> None:
        """FP-0043 Stage 5: re-point the on-disk snapshot path post-construction.

        spawn_session uses this so a spawned session persists to its OWN per-session
        location (``<state>/sessions/<sid>/snapshot.json``) instead of colliding with
        the agent's "main" snapshot. Pre-live (before any append/save), so no entry
        is ever written to the wrong path."""
        self._snapshot_path = Path(snapshot_path)

    def set_generation_store(self, generation_store) -> None:
        """FP-0043 Stage 5: re-point the PITR generation store post-construction.

        spawn_session uses this so a spawned session's generations land in its own
        per-session ``generations`` dir (paired with set_snapshot_path). None →
        generation cuts become no-ops (unchanged from the no-store default)."""
        self._generation_store = generation_store

    def set_anchor_store(self, anchor_store) -> None:
        """Attach the shared per-checkpoint anchor store (#1547).

        Injected by the registry so the capture seam (cut_generation) and the
        timeline surface (list_rewind_points) share one store keyed by WAL seq.
        """
        self._anchor_store = anchor_store

    async def cut_generation(self, anchor: str = "", full_message: str = "") -> None:
        """Record the current snapshot as a PITR generation (ADR-0038 Stage 1a).

        Called at user-facing checkpoint boundaries (turn / plan-step) — a
        single seam so cuts are neither missed nor doubled. Records the runtime
        AgentSnapshot generation tied at the boundary seq (= ``snapshot.applied_seq``,
        a WAL seq). Additive to the per-mutation ``save()``. No-op when no
        generation store / WAL is configured.

        ``anchor`` (#1547): the truncated last user message for the rewind-timeline
        preview, captured against the same boundary seq. Empty / no anchor store →
        skipped (turn boundaries pass it; plan-step / phase cuts leave it empty).
        ``full_message`` (#1533 2c): the full original user message for the
        edit-prefill, persisted alongside the truncated anchor (turn boundaries
        only — same as ``anchor``).
        """
        if self._generation_store is None or self._state_log is None:
            return
        # #2259 PR-2b: capture the content SYNC (consistent at this boundary), then record the
        # generation in a worker job that stamps applied_seq from the worker-assigned seq — so
        # the gen is (content-at-cut, seq-at-cut), never a live snapshot whose content is AHEAD
        # of its (last-durable) applied_seq (which a rewind would replay+double-apply). The job
        # is FIFO-after the pre-cut mutations' WAL jobs and before any post-cut one, so
        # last_assigned_seq = the last pre-cut mutation's seq, matching the captured content.
        payload = copy.deepcopy(self._snapshot.to_payload())
        store = self._generation_store
        log = self._state_log
        anchor_store = self._anchor_store

        async def _record() -> None:
            seq = log.last_assigned_seq
            payload["applied_seq"] = seq
            store.record_payload(payload, seq)
            if anchor_store is not None and anchor:
                anchor_store.capture(seq, anchor, full=full_message)

        log.submit_durable_nowait(_record)

    async def flush(self) -> None:
        """#2259 PR-2b: drain every enqueued durable write (WAL + snapshot + gen) for this
        journal's worker, WITHOUT closing it — a barrier so a caller (or test) can observe a
        fire-and-forget mutation's durable effect (applied_seq stamped, snapshot/gen on disk).
        No-op without a WAL."""
        if self._state_log is not None:
            await self._state_log.flush()

    # ── public read access ────────────────────────────────────────────────

    @property
    def snapshot(self) -> AgentSnapshot:
        """Current in-memory snapshot (read-only view; mutate via methods)."""
        return self._snapshot

    # ── WAL-recorded mutations ────────────────────────────────────────────

    async def append_inbox(self, *, kind: str, payload: dict) -> str:
        """Append ``inbox_put`` to WAL, update snapshot, return assigned msg_id.

        Mirrors ``Session._put_inbox``.  Note: this method does NOT
        queue the message onto the asyncio inbox — the caller (session) is
        responsible for ``inbox.put`` so that the queue ownership stays in
        the session layer.
        """
        msg_id = uuid.uuid4().hex[:8]
        full_payload = {**payload, "_msg_id": msg_id}
        if self._state_log is not None:
            self._wal_append_nowait(
                "inbox_put", target=self._agent_name,
                msg_id=msg_id, msg_kind=kind, payload=full_payload,
            )
            self._snapshot.inbox.append({
                "id": msg_id, "kind": kind, "payload": full_payload,
            })
            self.save_nowait()
        return msg_id

    async def consume_inbox(self, *, msg_id: str) -> None:
        """Append ``inbox_consume`` to WAL and prune the snapshot entry.

        Mirrors the WAL/snapshot portion of ``Session._consume_inbox``.
        No-op when ``state_log is None`` or ``msg_id`` is ``None``.
        """
        if self._state_log is None or msg_id is None:
            return
        self._wal_append_nowait(
            "inbox_consume", agent=self._agent_name, msg_id=msg_id,
        )
        self._snapshot.inbox = [
            m for m in self._snapshot.inbox if m.get("id") != msg_id
        ]
        self.save_nowait()

    async def record_chain_register(self, *, chain_id: str, fields: dict) -> None:
        """Append ``chain_register`` to WAL and create the pending_chains entry.

        Mirrors ``Session._record_chain_register``.  ``fields`` must
        contain the chain metadata keys produced by the caller (origin_agent,
        origin_depth, original_request, waiting_on).
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "chain_register", agent=self._agent_name, chain_id=chain_id,
            **fields,
        )
        self._snapshot.pending_chains[chain_id] = {
            "chain_id": chain_id,
            **{k: (list(v) if k == "waiting_on" else v) for k, v in fields.items()},
        }
        self.save_nowait()

    async def record_chain_update(self, *, chain_id: str, fields: dict) -> None:
        """Append ``chain_update`` to WAL and update waiting_on in snapshot.

        Mirrors ``Session._record_chain_update``.  ``fields`` must
        contain at least ``waiting_on: list[str]``.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "chain_update", agent=self._agent_name, chain_id=chain_id,
            **fields,
        )
        chain = self._snapshot.pending_chains.get(chain_id)
        if chain is not None:
            waiting_on = fields.get("waiting_on", [])
            chain["waiting_on"] = list(waiting_on)
        self.save_nowait()

    async def record_chain_resolve(self, *, chain_id: str) -> None:
        """Append ``chain_resolve`` to WAL and remove the pending_chains entry.

        Mirrors ``Session._record_chain_resolve``.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "chain_resolve", agent=self._agent_name, chain_id=chain_id,
        )
        self._snapshot.pending_chains.pop(chain_id, None)
        self.save_nowait()

    async def record_chain_timeout_fired(self, *, chain_id: str) -> None:
        """Append ``chain_timeout_fired`` to WAL and remove the pending_chains entry.

        Mirrors ``Session._record_chain_timeout_fired``.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "chain_timeout_fired", agent=self._agent_name, chain_id=chain_id,
        )
        self._snapshot.pending_chains.pop(chain_id, None)
        self.save_nowait()

    # ── intervention persistence (PR-intervention-link L2) ────────────────

    async def record_intervention_dispatched(
        self, *, intervention_id: str, iv_dict: dict,
    ) -> None:
        """Append ``intervention_dispatched`` to WAL + add to outstanding.

        Called when a UserIntervention has been queued and announced to the
        user. Crash between this call and the user answering means resume
        will re-enqueue the intervention from the snapshot — the original
        the run can then await the answer once it's delivered.

        ``iv_dict`` should be the result of ``UserIntervention.to_dict()``
        (excludes the volatile ``future`` field).
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "intervention_dispatched",
            target=self._agent_name,
            intervention_id=intervention_id,
            iv_dict=iv_dict,
        )
        self._snapshot.outstanding_interventions[intervention_id] = iv_dict
        self.save_nowait()

    async def record_intervention_resolved(
        self, *, intervention_id: str,
    ) -> None:
        """Append ``intervention_resolved`` to WAL + drop from outstanding.

        Idempotent — pop is a no-op when the entry is already gone (e.g.
        duplicate WAL replay during recovery).
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "intervention_resolved",
            target=self._agent_name,
            intervention_id=intervention_id,
        )
        self._snapshot.outstanding_interventions.pop(intervention_id, None)
        self.save_nowait()

    async def record_intervention_answer_buffered(
        self, *, run_id: str, text: str, choice_id: str | None,
    ) -> None:
        """Append ``intervention_answer_buffered`` to WAL + add to buffer (R-D12).

        Called when the user answers a restored intervention post-restart
        but before the resuming run consumes the answer. Persisting
        this to the WAL+snapshot lets the answer survive a second crash
        (the buffer would otherwise be lost since it lives in
        Session's in-memory dict).
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "intervention_answer_buffered",
            target=self._agent_name,
            run_id=run_id,
            text=text,
            choice_id=choice_id,
        )
        self._snapshot.buffered_intervention_answers[run_id] = {
            "text": text, "choice_id": choice_id,
        }
        self.save_nowait()

    async def record_intervention_answer_consumed(
        self, *, run_id: str,
    ) -> None:
        """Append ``intervention_answer_consumed`` to WAL + drop from buffer (R-D12).

        Called when the resuming run consumes a buffered answer, OR
        when ``_drop_interventions_for_run`` clears the run's state. The
        consumed event prunes the durable buffer entry so a future
        restart doesn't see a stale answer.

        Idempotent: pop is a no-op if already gone.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "intervention_answer_consumed",
            target=self._agent_name,
            run_id=run_id,
        )
        self._snapshot.buffered_intervention_answers.pop(run_id, None)
        self.save_nowait()

    async def record_next_turn_context_staged(
        self, *, kind: str, payload: dict,
    ) -> None:
        """Append ``next_turn_context_staged`` to WAL + add entry to buffer (#1800-4b).

        Called when a wake=false ride-along is drained and staged for the
        next turn.  Persisting durably (decision B) ensures the entry
        survives a crash while the session waits for the trigger message.
        """
        if self._state_log is None:
            return
        entry = {"kind": kind, "payload": payload}
        self._wal_append_nowait(
            "next_turn_context_staged",
            target=self._agent_name,
            entry=entry,
        )
        self._snapshot.next_turn_context.append(entry)
        self.save_nowait()

    # ── loop-valve counter (#2884) ──────────────────────────────────────────

    async def record_hook_driven_turns(self, *, count: int) -> None:
        """Append ``hook_driven_turns_set`` to WAL + update the snapshot (#2884).

        Mirrors ``record_intervention_answer_buffered``: Session's in-memory
        ``_hook_driven_turns`` (the loop-valve counter bounding hook
        self-continuation) is the runtime cache; ``AgentSnapshot.hook_driven_turns``
        is its on-disk durable form. Records the FULL current value (not a
        delta) at both the reset-to-0 (kind="user") and each increment
        (kind="hook") edges, so replay (``AgentSnapshot._apply_one``) can
        reconstruct the exact value between snapshots by replaying the same
        WAL kind — no dependency on ``inbox_consume`` carrying the item kind.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "hook_driven_turns_set", target=self._agent_name, count=count,
        )
        self._snapshot.hook_driven_turns = count
        self.save_nowait()

    async def record_next_turn_context_cleared(self) -> None:
        """Append ``next_turn_context_cleared`` to WAL + clear the buffer (#1800-4b).

        Called after the staged entries are injected into history at the
        start of the trigger's turn.  Clearing durably prevents re-injection
        on a crash+restore that happens mid-turn.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "next_turn_context_cleared",
            target=self._agent_name,
        )
        self._snapshot.next_turn_context.clear()
        self.save_nowait()

    # ── restore / persist ─────────────────────────────────────────────────

    def install(self, snapshot: AgentSnapshot) -> None:
        """Adopt a recovered snapshot and immediately persist it.

        Mirrors the WAL/snapshot install portion of
        ``Session.restore_state`` (the asyncio queue repopulation and
        chain timeout re-arming remain the session's responsibility).

        Persists synchronously: restore is a one-shot recovery write (not the hot
        per-mutation path), so it keeps the original sync save rather than forcing an
        async restore path. The off-loop routing (#1765 1a-ii) is for the frequent
        per-mutation ``save()`` that would otherwise freeze the loop.
        """
        self._snapshot = snapshot
        self._snapshot.save(self._snapshot_path)

    async def save(self) -> None:
        """Persist the current snapshot to disk (atomic write via AgentSnapshot).

        #1765 Step 1a-ii: the snapshot is SERIALISED synchronously here — capturing a
        consistent view of the mutable state (inbox / chains / …) at this instant — and only
        the durable write+fsync is routed OFF the event loop, through the SAME serial
        DurabilityWorker as the WAL (``state_log.submit_durable``). Two guarantees follow:

        * **Loop-free fsync** — the snapshot fsync no longer freezes the event loop.
        * **WAL → snapshot ordering** — every mutation method awaits its WAL append (durable)
          BEFORE awaiting this save, and the worker is serial FIFO, so the snapshot's
          ``applied_seq`` becomes durable only AFTER the WAL seq it records
          (``applied_seq`` ≤ durable WAL seq). A crash can never leave a durable snapshot
          pointing at a non-durable WAL entry.

        No WAL (``state_log is None``: tests / non-chat) → the original synchronous save, so
        the no-persistence contract is byte-identical. No try/except — I/O errors propagate
        (unchanged from the original)."""
        if self._state_log is None:
            self._snapshot.save(self._snapshot_path)
            return
        data = self._snapshot.serialize()  # sync: consistent state captured before any await
        path = self._snapshot_path

        async def _write() -> None:
            await asyncio.to_thread(AgentSnapshot.write_durable, path, data)

        await self._state_log.submit_durable(_write)

    def save_nowait(self) -> None:
        """#2259 PR-2b: persist the snapshot NON-BLOCKING — the fire-and-forget counterpart of
        `save()`, paired with `_wal_append_nowait`.

        (1) DEEP-COPY the payload SYNCHRONOUSLY (serialize-sync-at-submit, criterion #3 — a
        consistent view of the mutable state at this instant, immune to a later in-place mutation
        e.g. `chain["waiting_on"]=…`); (2) in the durable JOB, stamp `applied_seq` from
        `state_log.last_assigned_seq` — the seq the PAIRED `_wal_append_nowait`'s WAL job assigned
        IN THE WORKER. The pair was enqueued atomically (the journal mutation calls
        `_wal_append_nowait` then `save_nowait` with NO await between), so the worker's FIFO runs
        WAL_N then snap_N with no other WAL job between → snap_N reads WAL_N's seq, never a later
        one (invariant #2), and the seq is worker-assigned (a durable WAL seq, never a non-durable
        sync value — the hole the sync-seq had). (3) fire-and-forget through the SAME serial worker
        AFTER the WAL append (FIFO lag → applied_seq ≤ durable-WAL-seq, criterion #1; a crash
        mid-pair → recovery replays the WAL entry onto the prior snapshot = consistent prefix,
        criterion #2). The hot path NEVER awaits durability (the blocking-invariant).

        No WAL (`state_log is None`: tests / non-chat) → the original synchronous save."""
        if self._state_log is None:
            self._snapshot.save(self._snapshot_path)
            return
        payload = copy.deepcopy(self._snapshot.to_payload())  # sync consistent capture
        path = self._snapshot_path
        log = self._state_log
        snapshot = self._snapshot

        async def _write() -> None:
            seq = log.last_assigned_seq  # worker-assigned seq, read in the job (after the WAL job)
            payload["applied_seq"] = seq
            await asyncio.to_thread(
                AgentSnapshot.write_durable, path, AgentSnapshot.serialize_payload(payload),
            )
            # #2259 PR-2b: track the DURABLE seq on the in-memory snapshot AFTER the write — the
            # truncate floor (`compute_truncate_floor` → min agent applied_seq) reads this in-memory
            # value, so it must equal the last-DURABLE seq (never ahead of durability, or the floor
            # could drop a WAL entry a not-yet-durable snapshot still needs). Set post-write = lags
            # toward durable = conservative + correct. Atomic int assign on the loop (no race).
            snapshot.applied_seq = seq

        log.submit_durable_nowait(_write)
