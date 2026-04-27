from __future__ import annotations
from typing import Callable
from .models import Event


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
