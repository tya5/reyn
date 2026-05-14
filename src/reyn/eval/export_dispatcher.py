"""EventExportDispatcher — EventLog subscriber that drives TraceExporter backends.

Wires into the EventLog subscriber pattern (same as ChatEventForwarder and
EventStore) to dispatch P6 events to configured exporters after a skill run
finishes.

Dispatch fires on ``workflow_finished`` — at that point the EventLog holds the
complete event sequence for the run.  All events collected up to that point
are exported in a single batch.

Failures in any individual exporter are swallowed at WARNING level; they MUST
NOT propagate to the caller or interfere with the skill run result (P6 audit
path is independent of the export side channel).

P7 compliance: this module references only the OS-level event type
``workflow_finished`` — that is an OS vocabulary term, not a skill-specific
string (it is declared in event_schema.EVENT_AUDIT_REQUIREMENTS).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from reyn.schemas.models import Event

if TYPE_CHECKING:
    from reyn.eval.export import TraceExporter
    from reyn.events.events import EventLog

logger = logging.getLogger(__name__)

# The OS-level event kind that signals a skill run has finished.
# This is an OS-vocabulary term (defined in EVENT_AUDIT_REQUIREMENTS),
# not a skill-specific identifier — P7 is not violated.
_WORKFLOW_FINISHED = "workflow_finished"


class EventExportDispatcher:
    """EventLog subscriber: on workflow_finished, dispatch all run events to exporters.

    Usage:
        dispatcher = EventExportDispatcher(exporters=[...], event_log=run_events)
        event_log.add_subscriber(dispatcher)

    The dispatcher stores a reference to the EventLog so it can call
    ``event_log.to_json()`` when the terminal event fires.
    """

    def __init__(
        self,
        exporters: list[TraceExporter],
        event_log: EventLog,
    ) -> None:
        self._exporters = list(exporters)
        self._event_log = event_log

    def __call__(self, event: Event) -> None:
        """Called synchronously by EventLog.emit().

        When the terminal workflow_finished event arrives, kick off an
        async export task.  We schedule it on the running event loop
        so as not to block the emit() call path.  If there is no running
        loop (e.g. in a test driving the runtime synchronously) we run
        the coroutine directly in a new loop.
        """
        if event.type != _WORKFLOW_FINISHED:
            return
        events_json = self._event_log.to_json()
        if not events_json:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._dispatch(events_json))
        except RuntimeError:
            # No running loop — run synchronously (test / CLI path).
            asyncio.run(self._dispatch(events_json))

    async def _dispatch(self, events_json: list[dict]) -> None:
        """Dispatch events to all configured exporters concurrently."""
        tasks = [self._safe_export(exp, events_json) for exp in self._exporters]
        await asyncio.gather(*tasks)

    async def _safe_export(self, exporter: TraceExporter, events_json: list[dict]) -> None:
        """Run a single exporter; swallow all exceptions at WARNING level."""
        try:
            await exporter.export(events_json)
        except Exception as exc:
            logger.warning(
                "EventExportDispatcher: exporter %s failed: %s",
                type(exporter).__name__, exc,
            )
