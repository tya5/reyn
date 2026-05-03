"""SnapshotJournal — owns AgentSnapshot + StateLog WAL (extracted from ChatSession wave 1).

All WAL-recorded mutations go through here; in-memory readers go via the
`.snapshot` property.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from reyn.events.agent_snapshot import AgentSnapshot
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
    ) -> None:
        self._agent_name = agent_name
        self._snapshot_path = Path(snapshot_path)
        self._state_log = state_log
        self._snapshot: AgentSnapshot = AgentSnapshot.empty(agent_name)

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
