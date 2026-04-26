from .models import Event


class EventLog:
    def __init__(self) -> None:
        self._events: list[Event] = []

    def emit(self, type: str, **data) -> Event:
        event = Event(type=type, data=data)
        self._events.append(event)
        return event

    def all(self) -> list[Event]:
        return list(self._events)

    def to_json(self) -> list[dict]:
        return [e.model_dump(mode="json") for e in self._events]
