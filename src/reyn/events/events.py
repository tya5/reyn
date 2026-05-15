from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Callable

from reyn.schemas.models import Event

logger = logging.getLogger(__name__)


class EventLog:
    def __init__(self, subscribers: list[Callable[[Event], None]] | None = None) -> None:
        self._events: list[Event] = []
        self._subscribers: list[Callable[[Event], None]] = list(subscribers or [])

    @property
    def subscribers(self) -> list[Callable[[Event], None]]:
        return self._subscribers

    def add_subscriber(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def emit(self, type: str, **data) -> Event:
        event = Event(type=type, data=data)
        self._events.append(event)
        for sub in self._subscribers:
            sub(event)
        return event

    def all(self) -> list[Event]:
        return list(self._events)

    def to_json(self) -> list[dict]:
        return [e.model_dump(mode="json") for e in self._events]


def _find_reyn_dir(start: Path) -> Path | None:
    """Walk up from *start* until finding a directory containing `.reyn/`, or return None."""
    current = start.resolve()
    while True:
        candidate = current / ".reyn"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def emit_cli_event(kind: str, **payload) -> None:
    """Emit a one-off P6 event from a CLI context (no active session).

    Routes to ``.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl``. Locates the
    ``.reyn/`` dir by walking up from ``Path.cwd()``. If no ``.reyn/``
    directory is found, logs a warning and returns silently — the caller's
    operation is the primary action; audit-emit failure must not propagate.

    The file is appended to (P6 append-only contract). Dir creation is
    idempotent (``mkdir(parents=True, exist_ok=True)``).
    """
    from reyn.events.event_store import EventStore

    reyn_dir = _find_reyn_dir(Path.cwd())
    if reyn_dir is None:
        logger.warning(
            "emit_cli_event: no .reyn/ directory found from %s; "
            "skipping P6 audit emit for event %r",
            Path.cwd(),
            kind,
        )
        return

    cli_dir = reyn_dir / "events" / "direct" / "cli"
    today = date.today().isoformat()  # YYYY-MM-DD
    # Use a date-named suffix so each day's CLI events land in one predictable file.
    # max_bytes=0 / max_age_seconds=0 disables rotation — the suffix IS the date.
    store = EventStore(cli_dir, max_bytes=0, max_age_seconds=0, suffix=f"_{today}")
    event_log = EventLog(subscribers=[store])
    event_log.emit(kind, **payload)
