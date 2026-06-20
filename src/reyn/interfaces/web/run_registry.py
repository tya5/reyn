"""A2A task lifecycle registry (FP-0001 + issue #267 Gap 5 persistence).

Tracks asyncio.Task instances spawned by POST /a2a/agents/<name> async-mode
calls. Each entry carries A2A-task-wrapper-owned state (status, result,
error, webhook URL, SSE event buffer). Concurrent access by FastAPI
request handlers is serialised by ``RunRegistry`` internally; do NOT
call from outside the asyncio event loop.

Persistence (issue #267 Gap 5):

When ``RunRegistry`` is constructed with a ``persist_path``, every
mutation rewrites the file atomically (= tmp file + ``Path.replace()``)
so a server-process restart can reload the registry from disk.

Persistence boundary (= issue #292 α refactor):

Pre-#292, ``RunEntry`` also persisted ``pending_intervention`` (= the
full ``UserIntervention`` object). That field has been **removed**:
the iv is owned by ``Session._interventions`` (= same machinery
TUI ivs use, including R-D12's persistent answer buffer). What
``RunRegistry`` persists is only the A2A-task-wrapper state.

What persists:
  - run_id, agent_name, chain_id, status, result, error
  - webhook_url, history_events, created_at, updated_at

What does NOT persist (= volatile, restored as ``None`` / dropped):
  - asyncio.Task reference (= bound to the process that died)
  - pending iv state (= owned by Session; queried at request time
    rather than mirrored here)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.channel_state import ChannelState

logger = logging.getLogger(__name__)


@dataclass
class RunEntry:
    """One A2A async task. Mutable; ``RunRegistry`` owns lifecycle.

    issue #292 (α): ``pending_intervention`` and ``question`` fields
    removed — iv state lives in ``Session._interventions``. This
    entry only carries A2A-task-wrapper state.

    issue #269 Phase 2: ``_webhook_channel_state`` (private, lazy-init)
    tracks dead-channel detection for the registered ``webhook_url``.
    See ``RunRegistry.webhook_channel_state``.
    """
    run_id: str
    agent_name: str
    chain_id: str
    status: str = "running"
    result: str | None = None
    error: str | None = None
    webhook_url: str | None = None
    # #1814: the core session routing-key (``<transport>:<native_id>``, e.g.
    # ``a2a:<contextId>``) this run belongs to — the same neutral session identity
    # ``registry.resolve_session`` produces for every transport. Carried so the
    # escalation monitor / answer-injection / completion-narration drain resolve
    # the SAME session as the originating request. None for pre-#1814 entries. The
    # A2A layer owns the ``contextId ↔ session_id`` mapping — core stays term-neutral.
    session_id: str | None = None
    task: asyncio.Task | None = None
    history_events: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # issue #269 Phase 2: per-run webhook liveness state. In-memory
    # only (not persisted) — restart starts fresh, which matches the
    # peer's expectation that a server restart resets retry counters.
    _webhook_channel_state: "ChannelState | None" = field(
        default=None, repr=False,
    )

    def to_public_dict(self) -> dict:
        """JSON-safe shape for GET /a2a/tasks/{run_id} responses.
        Drops asyncio.Task (= internal-only)."""
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "chain_id": self.chain_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_persist_dict(self) -> dict:
        """Persistence-safe shape (issue #267 Gap 5 + issue #292 α).

        Includes webhook_url + history_events for post-restart peer
        notification + SSE replay continuity. iv state (formerly
        ``pending_intervention``) is owned by Session and persisted
        via AgentSnapshot — see issue #292 body for the layering.

        Excludes volatile fields:
          - ``task``: asyncio.Task is bound to the dead process
        """
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "chain_id": self.chain_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "webhook_url": self.webhook_url,
            "session_id": self.session_id,  # #1814
            "history_events": list(self.history_events),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_persist_dict(cls, data: dict) -> "RunEntry":
        """Inverse of ``to_persist_dict``.

        Rebuilds a ``RunEntry`` from the persistence snapshot. Tolerant
        of pre-#292 snapshots that included ``pending_intervention`` /
        ``question`` keys (= silently ignored, the iv is restored via
        Session's AgentSnapshot instead).
        """

        def _parse_ts(value: object) -> datetime:
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value)
                except ValueError:
                    pass
            return datetime.now(timezone.utc)

        return cls(
            run_id=str(data.get("run_id", "")),
            agent_name=str(data.get("agent_name", "")),
            chain_id=str(data.get("chain_id", "")),
            status=str(data.get("status", "running")),
            result=data.get("result"),
            error=data.get("error"),
            webhook_url=data.get("webhook_url"),
            session_id=data.get("session_id"),  # #1814 — None for pre-#1814 entries (graceful)
            task=None,
            history_events=list(data.get("history_events") or []),
            created_at=_parse_ts(data.get("created_at")),
            updated_at=_parse_ts(data.get("updated_at")),
        )


class RunRegistry:
    """In-memory ``run_id`` → ``RunEntry`` map for A2A async tasks.

    Single instance per Reyn server (attached to ``app.state.run_registry``
    by ``reyn.interfaces.web.server``).

    Persistence (issue #267 Gap 5): when constructed with a non-None
    ``persist_path``, every mutation atomically rewrites the file so a
    server-process restart can reload via ``__init__`` (which calls
    ``_restore_from`` if the file exists). Default ``None`` preserves
    pre-#267 in-memory-only behaviour for tests and direct callers
    that don't need persistence.
    """

    def __init__(self, *, persist_path: Path | None = None) -> None:
        self._runs: dict[str, RunEntry] = {}
        self._persist_path: Path | None = (
            Path(persist_path) if persist_path is not None else None
        )
        if self._persist_path is not None and self._persist_path.exists():
            self._restore_from(self._persist_path)

    # ── persistence (issue #267 Gap 5) ─────────────────────────────────────

    def _persist(self) -> None:
        """Atomically rewrite the snapshot file after a mutation.

        Snapshot shape: ``{run_id: entry.to_persist_dict(), ...}``. The
        atomic-rename pattern (= tmp file + ``Path.replace()``) avoids
        a half-written file being read by a concurrent restore.
        Persistence failures are logged but never re-raised — the
        registry stays usable even if the disk is full / read-only;
        restart will see whatever the last successful snapshot was.
        """
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                run_id: entry.to_persist_dict()
                for run_id, entry in self._runs.items()
            }
            tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._persist_path)
        except OSError as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "RunRegistry persist failed: path=%s exc=%s",
                self._persist_path, exc,
            )

    def _restore_from(self, path: Path) -> None:
        """Repopulate ``self._runs`` from the snapshot file.

        Tolerates a corrupt or partial file by logging a warning and
        leaving the registry empty rather than crashing — a fresh
        server can still accept new tasks; only resurrection fails.
        """
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "RunRegistry restore failed (= empty registry will be used): "
                "path=%s exc=%s", path, exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "RunRegistry snapshot is not a dict (= empty registry will "
                "be used): path=%s type=%s", path, type(data).__name__,
            )
            return
        for run_id, entry_data in data.items():
            if not isinstance(entry_data, dict):
                continue
            try:
                entry = RunEntry.from_persist_dict(entry_data)
            except Exception as exc:  # noqa: BLE001 — skip corrupt entries
                logger.warning(
                    "RunRegistry skipping corrupt entry run_id=%s exc=%s",
                    run_id, exc,
                )
                continue
            self._runs[str(run_id)] = entry

    # ── core CRUD (= mutations call _persist on success) ───────────────────

    def create(
        self,
        *,
        agent_name: str,
        chain_id: str,
        webhook_url: str | None = None,
        session_id: str | None = None,
    ) -> RunEntry:
        """Allocate a new run_id and entry with status='running'."""
        run_id = uuid.uuid4().hex
        entry = RunEntry(
            run_id=run_id,
            agent_name=agent_name,
            chain_id=chain_id,
            webhook_url=webhook_url,
            session_id=session_id,  # #1814
        )
        self._runs[run_id] = entry
        self._persist()
        return entry

    def get(self, run_id: str) -> RunEntry | None:
        return self._runs.get(run_id)

    def list(self, agent_name: str | None = None) -> list[RunEntry]:
        if agent_name is None:
            return list(self._runs.values())
        return [e for e in self._runs.values() if e.agent_name == agent_name]

    def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> RunEntry | None:
        """Update task-wrapper state. issue #292 (α): ``question`` and
        ``pending_intervention`` params removed — iv lifecycle is owned
        by Session.
        """
        entry = self._runs.get(run_id)
        if entry is None:
            return None
        if status is not None:
            entry.status = status
        if result is not None:
            entry.result = result
        if error is not None:
            entry.error = error
        entry.updated_at = datetime.now(timezone.utc)
        self._persist()
        return entry

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        if entry := self._runs.get(run_id):
            entry.task = task
        # NB: task is volatile (= not persisted), no _persist() call.

    def cancel(self, run_id: str) -> bool:
        """Cancel the task if running; mark status='cancelled'.
        Returns True iff the entry existed."""
        entry = self._runs.get(run_id)
        if entry is None:
            return False
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        entry.status = "cancelled"
        entry.updated_at = datetime.now(timezone.utc)
        self._persist()
        return True

    def append_event(self, run_id: str, event: dict) -> None:
        """Buffer an event for SSE replay (GET /a2a/tasks/{run_id}/events)."""
        entry = self._runs.get(run_id)
        if entry is not None:
            entry.history_events.append(event)
            self._persist()

    def remove(self, run_id: str) -> None:
        if self._runs.pop(run_id, None) is not None:
            self._persist()

    def webhook_channel_state(
        self, run_id: str,
    ) -> "ChannelState | None":
        """Get-or-create the ``ChannelState`` for this run's webhook URL.

        Returns ``None`` when the run has no ``webhook_url`` set (=
        peer never registered a callback URL → there's no channel to
        track). Otherwise lazy-initialises a ``ChannelState`` with
        ``channel_id="webhook:<run_id>"`` on first access.

        In-memory only — not persisted across process restart, which
        matches the peer's expectation that a restart resets retry
        counters. Callers (= ``A2AInterventionBus.on_dispatch``,
        ``_A2AProgressBridge._send``) check ``is_alive()`` before each
        fire and call ``record_attempt(result)`` after.

        issue #269 Phase 2.
        """
        entry = self._runs.get(run_id)
        if entry is None or entry.webhook_url is None:
            return None
        if entry._webhook_channel_state is None:
            from reyn.runtime.channel_state import ChannelState  # noqa: PLC0415
            entry._webhook_channel_state = ChannelState(
                channel_id=f"webhook:{run_id}",
            )
        return entry._webhook_channel_state


__all__ = ["RunEntry", "RunRegistry"]
