"""Tier 2: the plain ``--cui`` transcript's double-printed user line (#3287).

Repro (real TTY, ``reyn chat --cui``): a turn that goes through a real LLM
round-trip prints the user's own line TWICE — ``prompt_session.prompt_async``
(``run_input_loop``, ``stream_client.py``) already leaves ``you > <text>`` on
the terminal the instant Enter is pressed, and then the broadcast
``user_submitted`` chat-event this same submission produces re-renders it a
second time via ``renderer.on_chat_event`` (``run_output_loop``). A local
``/quit`` never reaches ``submit_user_text`` (short-circuited earlier in
``run_input_loop``), so it never emits a ``user_submitted`` event and never
doubled — exactly the asymmetry the bug report observed.

The fix threads an ``own_submissions`` FIFO (a plain ``deque``, owned by one
client's own input/output loop pair, never shared) between
``route_input_line`` (append on the ONE branch that actually calls
``submit_user_text``) and ``run_output_loop`` (match the broadcast event's
text against the queue head; on a hit, skip the redundant render and pop).
``client_driver.run_chat_client`` only constructs a non-``None`` queue when
``is_tty`` — a piped / non-interactive session has no terminal echo to
duplicate against, so the event-driven render stays the sole record there
(unaffected regression coverage below).

The FIFO is scoped per-client-loop-pair so a second attached client (which
never typed the first client's line, so its own terminal never echoed it)
still renders every ``user_submitted`` event normally — the ADR-0039 "every
attached client sees every turn" invariant survives (also covered below).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock/MagicMock/AsyncMock/patch on a collaborator. Renderer and
  transport doubles below are REAL instances of the actual base classes /a
  plain recorder, not stand-ins for something faked.
- No private-state assertions — behavior observed via the renderer's own
  recorded output and the queue's own (public, plain-deque) contents.
- Each test docstring's first line declares its Tier.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

import pytest

from reyn.interfaces.repl.stream_client import route_input_line, run_output_loop
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _Recorder:
    """A real renderer double: records every on_chat_event call it receives."""

    def __init__(self) -> None:
        self.events: list = []

    def message(self, msg: OutboxMessage) -> None:  # pragma: no cover - unused
        pass

    def on_chat_event(self, event) -> None:
        self.events.append(event)

    def uses_app_input(self) -> bool:
        return False


class _QueueTransport(ClientTransport):
    """A real, minimal ClientTransport: yields queued frames; records send-side
    calls plainly (no mocks) so a test can assert which branch fired."""

    def __init__(self, *, pending_head: object = None, answer_ok: bool = False) -> None:
        self._frames: "asyncio.Queue[object]" = asyncio.Queue()
        self._pending_head = pending_head
        self._answer_ok = answer_ok
        self.submitted: list[str] = []
        self.answered: list[str] = []

    async def push(self, frame) -> None:
        await self._frames.put(frame)

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._frames.get()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        if self._answer_ok:
            self.answered.append(text)
        return self._answer_ok

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover - unused
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return self._pending_head

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _user_submitted_event(text: str) -> Event:
    return Event(type="user_submitted", data={"text": text, "meta": {}})


# ---------------------------------------------------------------------------
# run_output_loop: the own-echo suppression + its non-vacuity / multi-client
# safety nets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_own_submission_is_not_rerendered_and_is_consumed() -> None:
    """Tier 2: a queued own-submission text matching the broadcast
    user_submitted event's text is NOT forwarded to renderer.on_chat_event
    (the terminal already showed it) and is popped off the FIFO."""
    transport = _QueueTransport()
    renderer = _Recorder()
    own_submissions: "deque[str]" = deque(["hello world test"])
    await transport.push(EventFrame(_user_submitted_event("hello world test")))
    await transport.push(EventFrame(Event(type="turn_started", data={})))
    await transport.push(EventFrame(Event(type="turn_settled", data={})))

    task = asyncio.create_task(
        run_output_loop(transport, renderer, own_submissions=own_submissions)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    rendered_types = [e.type for e in renderer.events]
    assert "user_submitted" not in rendered_types, (
        "the own-submission echo was re-rendered — the double-print bug"
    )
    assert "turn_started" in rendered_types  # other events still flow through
    assert not own_submissions, "the matched entry must be consumed from the FIFO"


@pytest.mark.asyncio
async def test_non_matching_event_still_renders_and_queue_untouched() -> None:
    """Tier 2: a user_submitted event whose text does NOT match this client's
    own queued submission (standing in for a SECOND attached client's turn,
    which this client's own terminal never echoed) still renders normally —
    the ADR-0039 "every attached client sees every turn" invariant."""
    transport = _QueueTransport()
    renderer = _Recorder()
    own_submissions: "deque[str]" = deque(["my own pending line"])
    await transport.push(EventFrame(_user_submitted_event("someone else's line")))

    task = asyncio.create_task(
        run_output_loop(transport, renderer, own_submissions=own_submissions)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    rendered_types = [e.type for e in renderer.events]
    assert "user_submitted" in rendered_types, (
        "another client's turn must still render — it was never echoed by MY terminal"
    )
    assert list(own_submissions) == ["my own pending line"], (
        "a non-matching event must not consume this client's own pending entry"
    )


@pytest.mark.asyncio
async def test_own_submissions_none_preserves_the_event_as_sole_echo() -> None:
    """Tier 2: non-vacuity + regression guard — own_submissions=None (the
    non-interactive / piped default) disables the check entirely, so the
    user_submitted event still renders (the piped path's only visible record
    of what was submitted, since nothing else echoes it)."""
    transport = _QueueTransport()
    renderer = _Recorder()
    await transport.push(EventFrame(_user_submitted_event("piped line")))

    task = asyncio.create_task(run_output_loop(transport, renderer, own_submissions=None))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "user_submitted" in [e.type for e in renderer.events]


# ---------------------------------------------------------------------------
# route_input_line: the FIFO is only populated on the branch that actually
# produces a user_submitted event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_input_line_appends_on_the_submit_user_text_branch() -> None:
    """Tier 2: an ordinary turn (no pending intervention) is delivered via
    submit_user_text, and its text is appended to own_submissions — the only
    branch run_output_loop can later match against."""
    transport = _QueueTransport(pending_head=None)
    own_submissions: "deque[str]" = deque()

    await route_input_line(transport, "hello world test", None, own_submissions=own_submissions)

    assert transport.submitted == ["hello world test"]
    assert list(own_submissions) == ["hello world test"]


@pytest.mark.asyncio
async def test_route_input_line_intervention_answer_does_not_append() -> None:
    """Tier 2: an intervention answer (delivered directly via
    answer_intervention_text, bypassing submit_user_text — #2690) is NOT
    appended to own_submissions. Strip this exclusion and a later ordinary
    turn's FIFO desyncs against a phantom entry that no user_submitted event
    will ever arrive to consume."""
    transport = _QueueTransport(pending_head=object(), answer_ok=True)
    own_submissions: "deque[str]" = deque()

    await route_input_line(transport, "y", None, own_submissions=own_submissions)

    assert transport.answered == ["y"]
    assert transport.submitted == []
    assert not own_submissions, (
        "an intervention answer must never be queued as a pending self-echo"
    )
