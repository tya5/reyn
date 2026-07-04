"""ExecutionDriver — structural Protocol for the per-turn router loop driver.

Defines the interface that ``Session._loop_driver`` must satisfy.  The
default implementation is ``RouterLoopDriver``; callers (Tier 2 tests,
future sub-session overrides) may inject a conforming alternative via
``Session(loop_driver=...)``.

Methods mirrored from ``RouterLoopDriver`` (the sole current implementor):

- ``run_turn(user_text, chain_id)`` — run one user turn through the router
  loop.
- ``is_cancel_requested()`` — poll the cooperative turn-cancel flag.
- ``request_cancel()`` — set the cancel flag + asyncio.Event.
- ``_check_cap(user_text)`` — enforce the per-turn router invocation cap.

The Protocol is ``runtime_checkable`` so ``isinstance()`` can be used in
assertion / diagnostic paths without importing the concrete class.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutionDriver(Protocol):
    """Structural interface for ``Session._loop_driver``.

    Every method signature here must stay in sync with the corresponding
    method on ``RouterLoopDriver``.  Drift is caught by the Tier 2 seam
    test (``tests/test_execution_driver_seam.py``).
    """

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        """Run RouterLoop for one user utterance."""
        ...

    def is_cancel_requested(self) -> bool:
        """Return True when a cooperative turn cancel has been requested."""
        ...

    def request_cancel(self) -> None:
        """Set the cooperative cancel flag and cancel_event."""
        ...

    async def _check_cap(self, user_text: str) -> None:
        """Enforce the per-turn router invocation cap."""
        ...
