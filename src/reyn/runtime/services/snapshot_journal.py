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
from reyn.runtime.turn_origin import TurnOrigin

# #6077 default WAL-append gate (see `_snapshot_interval`'s own comment in
# `__init__` for the replay-bound justification).
_DEFAULT_SNAPSHOT_INTERVAL = 20


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
    snapshot_interval:
        #6077: capture+write the snapshot once every this-many WAL appends
        (fire-and-forget path, see `save_nowait`). Defaults to
        `_DEFAULT_SNAPSHOT_INTERVAL`. A caller/test that needs a tighter or
        looser replay bound overrides it directly — this is the config knob
        half of the "no unexplained magic constant" requirement.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        snapshot_path: Path,
        state_log: StateLog | None,
        generation_store: SnapshotGenerationStore | None = None,
        session_id: str = "main",
        snapshot_interval: int = _DEFAULT_SNAPSHOT_INTERVAL,
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
        # #1547: per-checkpoint anchor text (the truncated last human
        # prompt — CLIENT_INPUT-origin; for a non-human turn, the most
        # recent one before it, #5648). Set post-construction by the
        # registry. None → no anchor capture.
        self._anchor_store = None
        self._snapshot: AgentSnapshot = AgentSnapshot.empty(agent_name, session_id)
        # #6077: WAL-count gate for `save_nowait` (see its own docstring). Counts
        # WAL appends SINCE the last snapshot capture+write; reset to 0 whenever a
        # capture actually runs (triggered here, or unconditionally in `close()`).
        self._wal_appends_since_snapshot = 0
        # #6077 default: capture+write once every 20 WAL appends. 20 is NOT a
        # measured value (owner: no measurement environment available, "go fix
        # theoretically-suspicious spots") — it is a replay-bound justification:
        # a crash leaves at most 19 trailing WAL entries un-snapshotted, all of
        # them small per-mutation deltas (inbox/chain/intervention dict updates,
        # never a bulk state rewrite), so replaying them onto the prior snapshot
        # (this module's criterion #2) is cheap regardless of process size — the
        # quantity this gate trades away is bounded structurally, not by timing.
        # Overridable per-instance (`snapshot_interval=`) for a caller/test that
        # needs a different bound without touching this constant.
        self._snapshot_interval = snapshot_interval

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

        ``anchor`` (#1547): the truncated last human prompt (CLIENT_INPUT-
        origin; for a non-human turn, the most recent one before it — #5648)
        for the rewind-timeline preview, captured against the same boundary
        seq. Empty / no anchor store → skipped (turn boundaries pass it;
        plan-step / phase cuts leave it empty).
        ``full_message`` (#1533 2c): the full original message this same
        source resolved to, for the edit-prefill, persisted alongside the
        truncated anchor (turn boundaries only — same as ``anchor``).
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

    async def append_inbox(self, *, kind: "TurnOrigin", payload: dict) -> str:
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

    async def cancel_inbox(self, *, msg_id: str) -> bool:
        """Cancel-by-id (#3300 P3 Y-server): append ``inbox_cancel`` to WAL and
        prune the snapshot entry, IFF ``msg_id`` is still present (undispatched).

        Returns ``True`` iff ``msg_id`` was found in ``snapshot.inbox`` and
        removed (the queued→cancelled transition actually happened); ``False``
        when absent — already dispatched (``consume_inbox`` already pruned it),
        already cancelled by a prior call (idempotent), or unknown. The caller
        (``Session.cancel_queued``) uses this boolean to decide whether to also
        emit the ``inbox_cancel`` audit-event delta and record the msg_id for
        skip-at-consume.

        ★§1 (CLAUDE.md recovery-feature gate / architect design-pass contract
        correction): the inbox is snapshot-backed (see module docstring +
        ``append_inbox``/``consume_inbox`` above), NOT purely WAL-event-derived
        — ``restore_all`` loads ``snapshot.json`` and replays only the WAL TAIL
        above its ``applied_seq``. A WAL ``inbox_cancel`` tombstone ALONE would
        therefore NOT survive a truncation below this item's ``inbox_put``
        event: replay would never see either event, and a snapshot that still
        held the (never-pruned) item would resurrect it. Pruning
        ``self._snapshot.inbox`` HERE, synchronously, at cancel-record time —
        exactly mirroring ``consume_inbox``'s shape — closes that window
        unconditionally (see ``tests/interfaces/test_3300_p3_cancel_by_id.py``'s
        truncate-falsify gate).

        ★F (no-await critical section, #3300 P3 design-pass pin F): this
        method is ``async def`` for call-site symmetry with ``append_inbox``/
        ``consume_inbox`` but is internally FULLY SYNCHRONOUS — the presence
        check, the WAL tombstone (``_wal_append_nowait``, fire-and-forget, no
        internal await), the snapshot prune (a plain list comprehension), and
        ``save_nowait`` (also fire-and-forget) never suspend. Awaiting this
        coroutine therefore never yields control back to the event loop, so no
        other task can interleave between the presence check and the commit —
        this is what makes ``Session.cancel_queued``'s "queued XOR dispatched"
        decision atomic (see that method's docstring for the full contract).
        """
        if self._state_log is None or msg_id is None:
            return False
        found = any(m.get("id") == msg_id for m in self._snapshot.inbox)
        if not found:
            return False
        self._wal_append_nowait(
            "inbox_cancel", agent=self._agent_name, msg_id=msg_id,
        )
        self._snapshot.inbox = [
            m for m in self._snapshot.inbox if m.get("id") != msg_id
        ]
        self.save_nowait()
        return True

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
        """Append ``chain_update`` to WAL and mirror every ``fields`` entry
        into the live in-memory snapshot.

        Mirrors ``Session._record_chain_update``. Every key in ``fields``
        is echoed to ``self._snapshot.pending_chains[chain_id]`` — not just
        ``waiting_on`` (a pre-#3978-P8 hardcode that silently dropped any
        OTHER field a caller passed from the live snapshot, even though it
        DID make it into the WAL event above: ``ChainManager.update()``'s
        own live ``_chains`` write is already generic over ``**fields``,
        so this mirror is the one place that wasn't). ``waiting_on``
        specifically needs list-coercion (sets aren't JSON-serializable);
        every other field is a plain scalar (``arm_at``, proposal 0067
        P8) and round-trips as-is.
        """
        if self._state_log is None:
            return
        self._wal_append_nowait(
            "chain_update", agent=self._agent_name, chain_id=chain_id,
            **fields,
        )
        chain = self._snapshot.pending_chains.get(chain_id)
        if chain is not None:
            for key, value in fields.items():
                chain[key] = list(value) if key == "waiting_on" else value
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

    # (#2884 added a `record_hook_driven_turns` method here for the
    # hook-driven-turns loop-valve counter's WAL/snapshot durability.
    # #5561 (owner ruling) retired the valve itself, and this method
    # with it — Session no longer calls it.)

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
        per-mutation ``save_nowait()`` that would otherwise freeze the loop.
        """
        self._snapshot = snapshot
        self._snapshot.save(self._snapshot_path)

    def _build_snapshot_write_job(self):
        """#6077: capture the payload SYNCHRONOUSLY (serialize-sync-at-submit, criterion #3 — a
        consistent view of the mutable state at this instant, immune to a later in-place mutation
        e.g. `chain["waiting_on"]=…`) and return the durable-worker job closure that stamps
        `applied_seq` from `state_log.last_assigned_seq` and writes it. Shared by the gated
        fire-and-forget path (`save_nowait`, submitted via `submit_durable_nowait`) and the
        unconditional shutdown path (`close`, submitted via `submit_durable` — awaited).

        Pairs with a preceding `_wal_append_nowait` call with NO await between (when used from
        `save_nowait`), so the worker's FIFO runs WAL_N then snap_N with no other WAL job between
        → snap_N reads WAL_N's seq, never a later one (invariant #2), and the seq is
        worker-assigned (a durable WAL seq, never a non-durable sync value)."""
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

        return _write

    def save_nowait(self) -> None:
        """#2259 PR-2b / #6077: persist the snapshot NON-BLOCKING — the fire-and-forget
        counterpart of the (now-deleted) synchronous ``save()``, paired with
        `_wal_append_nowait`.

        #6077: gated behind a WAL-APPEND-COUNT trigger (never a clock/timer — see the
        module docstring's crash-recovery criteria) — this is called from EVERY WAL-recorded
        mutation, so unconditionally capturing (`to_payload()` + `deepcopy`) and writing on
        every single call was doing full-state work on every append. Skips BOTH the capture
        and the write on the N-1 non-triggering calls (`_snapshot_interval`, see `__init__`);
        captures + enqueues only on the Nth. A crash between triggers replays the WAL tail
        onto the PRIOR snapshot — still a consistent prefix (this module's criterion #2),
        which is exactly why skipping is safe. `close()` (below) is the unconditional
        counterpart, called at shutdown so a clean exit never leaves a trailing gap.

        Fire-and-forget through the durability worker, AFTER the paired WAL append (FIFO lag →
        applied_seq ≤ durable-WAL-seq, criterion #1). #6077 提案 4 (strengthened from the earlier,
        await-only wording that let `deepcopy`/`json.dumps`/`open`/`write`/`close` sit on the
        loop unawaited): the blocking-invariant is now that the hot path neither AWAITS nor
        PERFORMS durability work — submit-and-proceed, checked by
        `tests/core/test_6077_hot_path_durability_boundary.py`'s AST-derived population (see that
        module's own SCOPE note for what it does and does not cover). `copy.deepcopy` in
        `_build_snapshot_write_job` below is CAPTURE (fixing a consistent view before a later
        in-place mutation), not durability — the invariant's line is drawn there, not at "no
        synchronous work at all" — and now, on non-triggering calls, does no work at all.

        No WAL (`state_log is None`: tests / non-chat) → the original synchronous save."""
        if self._state_log is None:
            self._snapshot.save(self._snapshot_path)
            return
        self._wal_appends_since_snapshot += 1
        if self._wal_appends_since_snapshot < self._snapshot_interval:
            return
        self._wal_appends_since_snapshot = 0
        self._state_log.submit_durable_nowait(self._build_snapshot_write_job())

    async def close(self) -> None:
        """#6077: unconditional final snapshot capture+write, independent of the N-append
        gate in `save_nowait` above.

        Without this, a clean shutdown could leave up to N-1 trailing WAL entries
        un-snapshotted (harmless for crash-recovery per se — criterion #2 still holds — but
        it means EVERY restart, not just a crash, pays a replay it didn't need to). Called at
        the same lifecycle point the shared `StateLog` itself closes (see
        `AgentRegistry.shutdown` for the ordering: journals close, THEN the shared worker
        closes, so this job is still guaranteed to run).

        AWAITS the durable write (unlike the fire-and-forget hot path) via `submit_durable` —
        the caller needs the write to have actually landed before the worker goes away, not
        merely enqueued. Resets the append counter so a re-used journal (tests) starts the
        next gate window fresh.

        No WAL (`state_log is None`: tests / non-chat) → the original synchronous save."""
        if self._state_log is None:
            self._snapshot.save(self._snapshot_path)
            return
        self._wal_appends_since_snapshot = 0
        await self._state_log.submit_durable(self._build_snapshot_write_job())
