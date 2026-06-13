"""SnapshotJournal — owns AgentSnapshot + StateLog WAL (extracted from ChatSession wave 1).

All WAL-recorded mutations go through here; in-memory readers go via the
`.snapshot` property.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.snapshot_generations import SnapshotGenerationStore
from reyn.events.state_log import StateLog


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
    ) -> None:
        self._agent_name = agent_name
        self._snapshot_path = Path(snapshot_path)
        self._state_log = state_log
        # ADR-0038 Stage 1a: PITR generation store. When None (tests /
        # non-chat), generation cuts are no-ops and the single snapshot.json
        # path is unchanged (no behavior change).
        self._generation_store = generation_store
        # ADR-0038 Stage 1d: the workspace half of a generation. Set post-
        # construction by the registry (the single shared shadow-git store) so
        # cut_generation captures workspace files at the SAME boundary as the
        # runtime snapshot. None → no workspace versioning (capture is skipped).
        self._workspace_store = None
        self._snapshot: AgentSnapshot = AgentSnapshot.empty(agent_name)

    def set_workspace_store(self, workspace_store) -> None:
        """Attach the shared workspace shadow-git store (ADR-0038 Stage 1d).

        Injected by the registry after session construction so the capture seam
        (cut_generation) and the rewind/recovery restore use the SAME git-dir.
        """
        self._workspace_store = workspace_store

    def cut_generation(self) -> None:
        """Record the current snapshot as a PITR generation (ADR-0038 Stage 1a/1d).

        Called at user-facing checkpoint boundaries (turn / plan-step) — a
        single seam so cuts are neither missed nor doubled. Records BOTH
        substrates tied at the boundary seq (= ``snapshot.applied_seq``, a WAL
        seq): the runtime AgentSnapshot generation AND (Stage 1d) the workspace
        shadow-git commit. Additive to the per-mutation ``save()``. No-op when no
        generation store / WAL is configured; workspace capture is skipped when
        no workspace store is attached (or git is unavailable — handled there).
        """
        if self._generation_store is None or self._state_log is None:
            return
        self._generation_store.record(self._snapshot)
        if self._workspace_store is not None:
            self._workspace_store.capture(self._snapshot.applied_seq)

    # ── public read access ────────────────────────────────────────────────

    @property
    def snapshot(self) -> AgentSnapshot:
        """Current in-memory snapshot (read-only view; mutate via methods)."""
        return self._snapshot

    # ── WAL-recorded mutations ────────────────────────────────────────────

    async def append_inbox(self, *, kind: str, payload: dict) -> str:
        """Append ``inbox_put`` to WAL, update snapshot, return assigned msg_id.

        Mirrors ``ChatSession._put_inbox``.  Note: this method does NOT
        queue the message onto the asyncio inbox — the caller (session) is
        responsible for ``inbox.put`` so that the queue ownership stays in
        the session layer.
        """
        msg_id = uuid.uuid4().hex[:8]
        full_payload = {**payload, "_msg_id": msg_id}
        if self._state_log is not None:
            seq = await self._state_log.append(
                "inbox_put", target=self._agent_name,
                msg_id=msg_id, msg_kind=kind, payload=full_payload,
            )
            self._snapshot.applied_seq = seq
            self._snapshot.inbox.append({
                "id": msg_id, "kind": kind, "payload": full_payload,
            })
            self.save()
        return msg_id

    async def consume_inbox(self, *, msg_id: str) -> None:
        """Append ``inbox_consume`` to WAL and prune the snapshot entry.

        Mirrors the WAL/snapshot portion of ``ChatSession._consume_inbox``.
        No-op when ``state_log is None`` or ``msg_id`` is ``None``.
        """
        if self._state_log is None or msg_id is None:
            return
        seq = await self._state_log.append(
            "inbox_consume", agent=self._agent_name, msg_id=msg_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.inbox = [
            m for m in self._snapshot.inbox if m.get("id") != msg_id
        ]
        self.save()

    async def record_chain_register(self, *, chain_id: str, fields: dict) -> None:
        """Append ``chain_register`` to WAL and create the pending_chains entry.

        Mirrors ``ChatSession._record_chain_register``.  ``fields`` must
        contain the chain metadata keys produced by the caller (origin_agent,
        origin_depth, original_request, waiting_on).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "chain_register", agent=self._agent_name, chain_id=chain_id,
            **fields,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.pending_chains[chain_id] = {
            "chain_id": chain_id,
            **{k: (list(v) if k == "waiting_on" else v) for k, v in fields.items()},
        }
        self.save()

    async def record_chain_update(self, *, chain_id: str, fields: dict) -> None:
        """Append ``chain_update`` to WAL and update waiting_on in snapshot.

        Mirrors ``ChatSession._record_chain_update``.  ``fields`` must
        contain at least ``waiting_on: list[str]``.
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "chain_update", agent=self._agent_name, chain_id=chain_id,
            **fields,
        )
        self._snapshot.applied_seq = seq
        chain = self._snapshot.pending_chains.get(chain_id)
        if chain is not None:
            waiting_on = fields.get("waiting_on", [])
            chain["waiting_on"] = list(waiting_on)
        self.save()

    async def record_chain_resolve(self, *, chain_id: str) -> None:
        """Append ``chain_resolve`` to WAL and remove the pending_chains entry.

        Mirrors ``ChatSession._record_chain_resolve``.
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "chain_resolve", agent=self._agent_name, chain_id=chain_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.pending_chains.pop(chain_id, None)
        self.save()

    async def record_chain_timeout_fired(self, *, chain_id: str) -> None:
        """Append ``chain_timeout_fired`` to WAL and remove the pending_chains entry.

        Mirrors ``ChatSession._record_chain_timeout_fired``.
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "chain_timeout_fired", agent=self._agent_name, chain_id=chain_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.pending_chains.pop(chain_id, None)
        self.save()

    # ── intervention persistence (PR-intervention-link L2) ────────────────

    async def record_intervention_dispatched(
        self, *, intervention_id: str, iv_dict: dict,
    ) -> None:
        """Append ``intervention_dispatched`` to WAL + add to outstanding.

        Called when a UserIntervention has been queued and announced to the
        user. Crash between this call and the user answering means resume
        will re-enqueue the intervention from the snapshot — the original
        skill_run can then await the answer once it's delivered.

        ``iv_dict`` should be the result of ``UserIntervention.to_dict()``
        (excludes the volatile ``future`` field).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "intervention_dispatched",
            target=self._agent_name,
            intervention_id=intervention_id,
            iv_dict=iv_dict,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.outstanding_interventions[intervention_id] = iv_dict
        self.save()

    async def record_intervention_resolved(
        self, *, intervention_id: str,
    ) -> None:
        """Append ``intervention_resolved`` to WAL + drop from outstanding.

        Idempotent — pop is a no-op when the entry is already gone (e.g.
        duplicate WAL replay during recovery).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "intervention_resolved",
            target=self._agent_name,
            intervention_id=intervention_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.outstanding_interventions.pop(intervention_id, None)
        self.save()

    async def record_intervention_answer_buffered(
        self, *, run_id: str, text: str, choice_id: str | None,
    ) -> None:
        """Append ``intervention_answer_buffered`` to WAL + add to buffer (R-D12).

        Called when the user answers a restored intervention post-restart
        but before the resuming skill consumes the answer. Persisting
        this to the WAL+snapshot lets the answer survive a second crash
        (the buffer would otherwise be lost since it lives in
        ChatSession's in-memory dict).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "intervention_answer_buffered",
            target=self._agent_name,
            run_id=run_id,
            text=text,
            choice_id=choice_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.buffered_intervention_answers[run_id] = {
            "text": text, "choice_id": choice_id,
        }
        self.save()

    async def record_intervention_answer_consumed(
        self, *, run_id: str,
    ) -> None:
        """Append ``intervention_answer_consumed`` to WAL + drop from buffer (R-D12).

        Called when the resuming skill consumes a buffered answer, OR
        when ``_drop_interventions_for_run`` clears the run's state. The
        consumed event prunes the durable buffer entry so a future
        restart doesn't see a stale answer.

        Idempotent: pop is a no-op if already gone.
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "intervention_answer_consumed",
            target=self._agent_name,
            run_id=run_id,
        )
        self._snapshot.applied_seq = seq
        self._snapshot.buffered_intervention_answers.pop(run_id, None)
        self.save()

    # ── plan-mode lifecycle persistence (ADR-0022 Phase 1) ────────────────

    async def record_plan_started(
        self, *, plan_id: str, goal: str, n_steps: int,
    ) -> None:
        """Append ``plan_started`` to WAL + add to active_plan_ids.

        Called at the top of ``execute_plan``. Crash between this call and
        ``record_plan_completed`` leaves the plan_id in active_plan_ids,
        which AgentRegistry.restore_all detects post-replay and treats as
        an interrupted plan (= cancel orphan child skills + notify user).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "plan_started",
            target=self._agent_name,
            plan_id=plan_id,
            goal=goal,
            n_steps=n_steps,
        )
        self._snapshot.applied_seq = seq
        if plan_id not in self._snapshot.active_plan_ids:
            self._snapshot.active_plan_ids.append(plan_id)
        self.save()

    async def record_plan_completed(self, *, plan_id: str) -> None:
        """Append ``plan_completed`` to WAL + remove from active_plan_ids.

        Idempotent — pop is a no-op when the entry is already gone (e.g.
        duplicate WAL replay during recovery).
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "plan_completed",
            target=self._agent_name,
            plan_id=plan_id,
        )
        self._snapshot.applied_seq = seq
        if plan_id in self._snapshot.active_plan_ids:
            self._snapshot.active_plan_ids.remove(plan_id)
        self.save()

    async def record_plan_aborted(
        self, *, plan_id: str, reason: str = "",
    ) -> None:
        """Append ``plan_aborted`` to WAL + remove from active_plan_ids.

        Used by AgentRegistry.restore_all post-replay cleanup of orphan
        plans, and (Phase 2) by explicit ``/plan discard`` user action.
        Idempotent.
        """
        if self._state_log is None:
            return
        seq = await self._state_log.append(
            "plan_aborted",
            target=self._agent_name,
            plan_id=plan_id,
            reason=reason,
        )
        self._snapshot.applied_seq = seq
        if plan_id in self._snapshot.active_plan_ids:
            self._snapshot.active_plan_ids.remove(plan_id)
        self.save()

    # ── plan-step lifecycle persistence (ADR-0023 Phase 2 step 4) ─────────
    #
    # Step events are promoted from events-log to WAL so the resume
    # analyzer can pair (plan_step_started, plan_step_completed |
    # plan_step_failed) deterministically across restart. Returns the
    # assigned WAL seq so the caller can stamp it onto PlanRegistry's
    # per-plan snapshot (PlanRegistry.record_step_*).

    async def record_plan_step_started(
        self, *, plan_id: str, step_id: str, depends_on: list[str],
        n_tools: int,
    ) -> int | None:
        """Append ``plan_step_started`` to WAL. Returns the assigned seq."""
        if self._state_log is None:
            return None
        seq = await self._state_log.append(
            "plan_step_started",
            target=self._agent_name,
            plan_id=plan_id,
            step_id=step_id,
            depends_on=list(depends_on),
            n_tools=n_tools,
        )
        self._snapshot.applied_seq = seq
        self.save()
        return seq

    async def record_plan_step_completed(
        self, *, plan_id: str, step_id: str, content_len: int,
    ) -> int | None:
        """Append ``plan_step_completed`` to WAL. Returns the assigned seq."""
        if self._state_log is None:
            return None
        seq = await self._state_log.append(
            "plan_step_completed",
            target=self._agent_name,
            plan_id=plan_id,
            step_id=step_id,
            content_len=content_len,
        )
        self._snapshot.applied_seq = seq
        self.save()
        # ADR-0038 Stage 1a: plan-step boundary = a user-facing checkpoint.
        self.cut_generation()
        return seq

    async def record_plan_step_failed(
        self, *, plan_id: str, step_id: str, error: str,
    ) -> int | None:
        """Append ``plan_step_failed`` to WAL. Returns the assigned seq."""
        if self._state_log is None:
            return None
        seq = await self._state_log.append(
            "plan_step_failed",
            target=self._agent_name,
            plan_id=plan_id,
            step_id=step_id,
            error=error,
        )
        self._snapshot.applied_seq = seq
        self.save()
        return seq

    # ── restore / persist ─────────────────────────────────────────────────

    def install(self, snapshot: AgentSnapshot) -> None:
        """Adopt a recovered snapshot and immediately persist it.

        Mirrors the WAL/snapshot install portion of
        ``ChatSession.restore_state`` (the asyncio queue repopulation and
        chain timeout re-arming remain the session's responsibility).
        """
        self._snapshot = snapshot
        self.save()

    def save(self) -> None:
        """Persist the current snapshot to disk (atomic write via AgentSnapshot.save).

        Mirrors ``ChatSession._save_snapshot``.  Follows the existing
        convention: no try/except — let I/O errors propagate (there is no
        error-suppression in the original implementation).
        """
        self._snapshot.save(self._snapshot_path)
