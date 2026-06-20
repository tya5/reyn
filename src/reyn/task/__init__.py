"""Task subsystem — first-class trackable work-units (#1953).

A Task is an opt-in, additive handle over a unit of work; the session model
(concurrent / interleaved / cross-chain) is unchanged. Completion is an
*explicit declaration* (an assignee calls ``task.update_status``), never an
inference from session state — which is why a Task can be tracked discretely
while the session keeps interleaving (#1953 §10).

This package holds the **term-neutral** domain model + the swappable backend
contract (P7: generic vocabulary; A2A terms like ``contextId`` / ``TaskState``
map only at the A2A layer). Slice 1 ships the contract + an in-memory backend;
sqlite (slice 2), single-writer CAS + P6 events (slice 3), and the rest follow.
"""
from __future__ import annotations

from reyn.task.backend import InMemoryTaskBackend, TaskBackend
from reyn.task.model import (
    TERMINAL_STATES,
    Task,
    TaskOrigin,
    TaskState,
)

__all__ = [
    "Task",
    "TaskState",
    "TaskOrigin",
    "TERMINAL_STATES",
    "TaskBackend",
    "InMemoryTaskBackend",
]
