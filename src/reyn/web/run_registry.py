"""A2A task lifecycle registry (FP-0001).

Tracks asyncio.Task instances spawned by POST /a2a/agents/<name> async-mode
calls. Each entry carries status, any pending ask_user intervention,
a webhook URL for push notifications, and a buffered event history
for SSE replay. Concurrent access by FastAPI request handlers is
serialised by ``RunRegistry`` internally; do NOT call from outside
the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.user_intervention import InterventionAnswer, UserIntervention

logger = logging.getLogger(__name__)


@dataclass
class RunEntry:
    """One A2A async task. Mutable; ``RunRegistry`` owns lifecycle."""
    run_id: str
    agent_name: str
    chain_id: str
    status: str = "running"
    question: str | None = None
    pending_intervention: "UserIntervention | None" = None
    result: str | None = None
    error: str | None = None
    webhook_url: str | None = None
    task: asyncio.Task | None = None
    history_events: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_public_dict(self) -> dict:
        """JSON-safe shape for GET /a2a/tasks/{run_id} responses.
        Drops asyncio.Task and UserIntervention (= internal-only)."""
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "chain_id": self.chain_id,
            "status": self.status,
            "question": self.question,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class RunRegistry:
    """In-memory ``run_id`` → ``RunEntry`` map for A2A async tasks.

    Single instance per Reyn server (attached to ``app.state.run_registry``
    by ``reyn.web.server``). Lifetime = process lifetime; the registry is
    intentionally not persisted (= crash-recovery for A2A async tasks is a
    follow-up FP).
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunEntry] = {}

    def create(
        self,
        *,
        agent_name: str,
        chain_id: str,
        webhook_url: str | None = None,
    ) -> RunEntry:
        """Allocate a new run_id and entry with status='running'."""
        run_id = uuid.uuid4().hex
        entry = RunEntry(
            run_id=run_id,
            agent_name=agent_name,
            chain_id=chain_id,
            webhook_url=webhook_url,
        )
        self._runs[run_id] = entry
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
        question: str | None = None,
        pending_intervention: "UserIntervention | None" = None,
        result: str | None = None,
        error: str | None = None,
    ) -> RunEntry | None:
        entry = self._runs.get(run_id)
        if entry is None:
            return None
        if status is not None:
            entry.status = status
        if question is not None:
            entry.question = question
        # pending_intervention can be set to None explicitly (= clearing)
        if pending_intervention is not None or status == "running":
            # status="running" after answer → also clear pending fields
            entry.pending_intervention = pending_intervention
        if result is not None:
            entry.result = result
        if error is not None:
            entry.error = error
        entry.updated_at = datetime.now(timezone.utc)
        return entry

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        if entry := self._runs.get(run_id):
            entry.task = task

    def answer_intervention(self, run_id: str, answer: "InterventionAnswer") -> bool:
        """Deliver ``answer`` to the run's pending intervention.

        Returns True iff the intervention was resolved (= run exists,
        has a pending intervention whose future isn't already done).
        After delivery, ``status`` returns to 'running' and ``question``
        is cleared.
        """
        entry = self._runs.get(run_id)
        if entry is None or entry.pending_intervention is None:
            return False
        iv = entry.pending_intervention
        if iv.future.done():
            return False
        iv.future.set_result(answer)
        entry.pending_intervention = None
        entry.question = None
        entry.status = "running"
        entry.updated_at = datetime.now(timezone.utc)
        return True

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
        return True

    def append_event(self, run_id: str, event: dict) -> None:
        """Buffer an event for SSE replay (GET /a2a/tasks/{run_id}/events)."""
        entry = self._runs.get(run_id)
        if entry is not None:
            entry.history_events.append(event)

    def remove(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


__all__ = ["RunEntry", "RunRegistry"]
