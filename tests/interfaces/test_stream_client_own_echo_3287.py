"""Tier 2: the plain ``--cui`` transcript's double-printed user line (#3287).

Repro (real TTY, ``reyn chat --cui``): a turn that goes through a real LLM
round-trip prints the user's own line TWICE — ``prompt_session.prompt_async``
(``run_input_loop``, ``stream_client.py``) already leaves ``you > <text>`` on
the terminal the instant Enter is pressed, and then the broadcast
``user_submitted`` audit-event this same submission produces re-renders it a
second time via ``renderer.on_audit_event`` (``run_output_loop``). A local
``/quit`` never reaches ``submit_user_text`` (short-circuited earlier in
``run_input_loop``), so it never emits a ``user_submitted`` event and never
doubled — exactly the asymmetry the bug report observed.

The fix threads an ``own_submissions`` set (owned by one client's own
input/output loop pair, never shared) between ``route_input_line`` (which
records the ``msg_id`` the transport's ``submit_user_text`` returns — the
SAME correlation id, #3300 P2a, the broadcast event carries) and
``run_output_loop`` (which matches the broadcast event's ``msg_id`` against
the set; on a hit, skip the redundant render and discard the entry).
``client_driver.run_chat_client`` only constructs a non-``None`` set when
``is_tty`` — a piped / non-interactive session has no terminal echo to
duplicate against, so the event-driven render stays the sole record there.

**Co-vet finding F1 (post-merge review on #3309)**: an earlier revision of
this fix matched by TEXT instead of by id. Two attached clients submitting
the SAME text (a short, common line like "yes") could then cross-match:
client A's own queued text satisfied by client B's broadcast (or vice
versa), simultaneously swallowing the OTHER client's line and leaving THIS
client's own line un-swallowed later — reintroducing the double-print bug
through a different door, and order-dependent on top of that. Matching by
``msg_id`` (a value #3300 P2a added SPECIFICALLY as a correlation id, never
form-sniffed from content) closes this: two different submissions never
share an id even when their text is identical, and per-client sets are never
shared, so this client's set only ever contains ids IT assigned. The
regression test below drives exactly that scenario: same text, two clients,
the OTHER client's broadcast arriving first.

**Co-vet finding F2 (same review round)**: F1's fix still left a REMOTE-only
gap, documented rather than closed: the msg_id only becomes visible to the
AG-UI client once its POST response returns, and the server may already have
pushed the SSE broadcast for the same submission over the INDEPENDENT events
connection in the interim — a network-ordering race between the two
channels. The reviewer pointed out this is unnecessary: the broadcast
``user_submitted`` event's ``meta.auth_connection_id`` (already wired by
#3300 for multi-client attribution — ``endpoint.py`` stamps it from the
POST's own query param, ``session.py`` copies ``meta`` verbatim onto the
event) is the client's OWN ``connection_id``, known client-side BEFORE any
submit even happens (``remote_client.py`` mints it with ``uuid.uuid4()`` at
startup). Matching on connection identity needs no second channel to
resolve, closing the race structurally rather than narrowing it. The tests
below drive: (1) suppression firing from ``meta.auth_connection_id`` alone,
with no ``own_submissions`` populated at all — proving the identity check
does not depend on msg_id timing; (2) a DIFFERENT connection_id (another
attached remote client) still rendering normally.

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock/MagicMock/AsyncMock/patch on a collaborator. Renderer and
  transport doubles below are REAL instances of the actual base classes /a
  plain recorder, not stand-ins for something faked.
- No private-state assertions — behavior observed via the renderer's own
  recorded output and the set's own (public, plain-set) contents.
- Each test docstring's first line declares its Tier.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.repl.stream_client import route_input_line, run_output_loop
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _Recorder:
    """A real renderer double: records every on_audit_event call it receives."""

    def __init__(self) -> None:
        self.events: list = []

    def message(self, msg: OutboxMessage) -> None:  # pragma: no cover - unused
        pass

    def on_audit_event(self, event) -> None:
        self.events.append(event)

    def uses_app_input(self) -> bool:
        return False


async def _wait_until(predicate) -> None:
    """#4264 ③: unbounded poll on run_output_loop's own OBSERVABLE effects
    (``own_submissions`` shrinking, ``renderer.events`` growing) — no fixed
    sleep, no attempt cap (testing.md § Time). No new production signal:
    both are state the loop already mutates for real consumers (the set it
    was handed, the renderer it renders into), not a hook added for this
    test's own sake."""
    while not predicate():
        await asyncio.sleep(0.01)


class _QueueTransport(ClientTransportStub):
    """A real, minimal ClientTransport: yields queued frames; ``submit_user_text``
    returns a caller-scripted msg_id (mirroring the production contract —
    #3287 — of returning the server-assigned correlation id), and records
    send-side calls plainly (no mocks) so a test can assert which branch fired."""

    def __init__(
        self,
        *,
        pending_head: object = None,
        answer_ok: bool = False,
        submit_msg_ids: "list[str] | None" = None,
    ) -> None:
        self._frames: "asyncio.Queue[object]" = asyncio.Queue()
        self._pending_head = pending_head
        self._answer_ok = answer_ok
        self._submit_msg_ids = list(submit_msg_ids or [])
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

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        if self._submit_msg_ids:
            return self._submit_msg_ids.pop(0)
        return ""

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


def _user_submitted_event(
    text: str, msg_id: str, *, auth_connection_id: "str | None" = None
) -> Event:
    meta = {"auth_connection_id": auth_connection_id} if auth_connection_id else {}
    return Event(
        type="user_submitted", data={"text": text, "meta": meta, "msg_id": msg_id}
    )


# ---------------------------------------------------------------------------
# run_output_loop: the own-echo suppression + its non-vacuity / multi-client
# safety nets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_own_submission_is_not_rerendered_and_is_consumed() -> None:
    """Tier 2: a queued own-submission msg_id matching the broadcast
    user_submitted event's msg_id is NOT forwarded to renderer.on_audit_event
    (the terminal already showed it) and is discarded from the set."""
    transport = _QueueTransport()
    renderer = _Recorder()
    own_submissions: "set[str]" = {"m-1"}
    await transport.push(EventFrame(_user_submitted_event("hello world test", "m-1")))
    await transport.push(EventFrame(Event(type="turn_started", data={})))
    await transport.push(EventFrame(Event(type="turn_settled", data={})))

    task = asyncio.create_task(
        run_output_loop(transport, renderer, own_submissions=own_submissions)
    )
    # #4264 ③: own_submissions emptying IS the loop's own observable effect
    # of having consumed the matching event — wait on that, not a sleep.
    await _wait_until(lambda: not own_submissions)
    # The other two queued frames (turn_started/turn_settled) still need a
    # moment to drain after the matching id is consumed.
    await _wait_until(lambda: "turn_settled" in [e.type for e in renderer.events])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    rendered_types = [e.type for e in renderer.events]
    assert "user_submitted" not in rendered_types, (
        "the own-submission echo was re-rendered — the double-print bug"
    )
    assert "turn_started" in rendered_types  # other events still flow through
    assert not own_submissions, "the matched id must be consumed from the set"


@pytest.mark.asyncio
async def test_non_matching_event_still_renders_and_set_untouched() -> None:
    """Tier 2: a user_submitted event whose msg_id does NOT match this
    client's own queued id (standing in for a SECOND attached client's turn,
    which this client's own terminal never echoed) still renders normally —
    the ADR-0039 "every attached client sees every turn" invariant."""
    transport = _QueueTransport()
    renderer = _Recorder()
    own_submissions: "set[str]" = {"m-mine"}
    await transport.push(EventFrame(_user_submitted_event("someone else's line", "m-theirs")))

    task = asyncio.create_task(
        run_output_loop(transport, renderer, own_submissions=own_submissions)
    )
    # #4264 ③: wait for the loop's own observable effect (the event actually
    # reaching the renderer) instead of a sleep.
    await _wait_until(lambda: "user_submitted" in [e.type for e in renderer.events])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    rendered_types = [e.type for e in renderer.events]
    assert "user_submitted" in rendered_types, (
        "another client's turn must still render — it was never echoed by MY terminal"
    )
    assert own_submissions == {"m-mine"}, (
        "a non-matching event must not consume this client's own pending id"
    )


@pytest.mark.asyncio
async def test_same_text_two_clients_does_not_cross_match_by_id() -> None:
    """Tier 2: co-vet F1 regression (#3309) — two attached clients submit the
    SAME text ("yes"), and the OTHER client's broadcast arrives FIRST. A
    text-matching implementation would treat the other client's identical
    text as satisfying THIS client's own queued entry — swallowing the other
    client's line (which THIS client's terminal never showed) and leaving
    THIS client's own entry stranded for a later, unrelated event to
    misconsume. Matching by msg_id must not exhibit either failure: the
    other client's differently-id'd event still renders, and this client's
    own entry (a DIFFERENT id, even though the text is identical) remains
    queued, ready to correctly match its own broadcast when it arrives."""
    transport = _QueueTransport()
    renderer = _Recorder()
    own_submissions: "set[str]" = {"m-mine"}
    # The OTHER client's identical-text turn, arriving first.
    await transport.push(EventFrame(_user_submitted_event("yes", "m-theirs")))
    # THIS client's own turn, arriving second.
    await transport.push(EventFrame(_user_submitted_event("yes", "m-mine")))

    task = asyncio.create_task(
        run_output_loop(transport, renderer, own_submissions=own_submissions)
    )
    # #4264 ③: own_submissions emptying means BOTH queued events have been
    # consumed — "m-theirs" (rendered) and "m-mine" (matched, suppressed) —
    # since the loop processes the FIFO queue one at a time.
    await _wait_until(lambda: not own_submissions)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    rendered_msg_ids = [
        e.data.get("msg_id") for e in renderer.events if e.type == "user_submitted"
    ]
    assert rendered_msg_ids == ["m-theirs"], (
        "the OTHER client's identically-texted turn must render exactly once "
        "(it was never echoed by MY terminal), and MY OWN turn must be "
        "suppressed (it WAS already echoed) — a text match would get at "
        "least one of these two wrong"
    )
    assert not own_submissions, "this client's own id is consumed once its OWN event arrives"


@pytest.mark.asyncio
async def test_own_submissions_none_preserves_the_event_as_sole_echo() -> None:
    """Tier 2: non-vacuity + regression guard — own_submissions=None (the
    non-interactive / piped default) disables the check entirely, so the
    user_submitted event still renders (the piped path's only visible record
    of what was submitted, since nothing else echoes it)."""
    transport = _QueueTransport()
    renderer = _Recorder()
    await transport.push(EventFrame(_user_submitted_event("piped line", "m-piped")))

    task = asyncio.create_task(run_output_loop(transport, renderer, own_submissions=None))
    # #4264 ③: wait for the loop's own observable effect instead of a sleep.
    await _wait_until(lambda: "user_submitted" in [e.type for e in renderer.events])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "user_submitted" in [e.type for e in renderer.events]


# ---------------------------------------------------------------------------
# run_output_loop: F2 — remote (AG-UI) own-echo suppression by connection
# identity, structurally race-free (no dependency on msg_id / POST-ack timing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_id_suppresses_with_no_msg_id_correlation_at_all() -> None:
    """Tier 2: co-vet F2 regression (#3309) — a user_submitted event whose
    meta.auth_connection_id matches THIS client's own connection_id is
    suppressed via own_connection_id ALONE. own_submissions is deliberately
    left empty/unpopulated (None) to prove the connection-identity check does
    not depend on ever learning a msg_id via the submit_user_text return value
    — i.e. no dependency on the POST-ack racing the SSE broadcast at all
    (unlike a msg_id-only scheme, which needs the id to have already arrived
    to match)."""
    transport = _QueueTransport()
    renderer = _Recorder()
    await transport.push(
        EventFrame(_user_submitted_event("hello", "m-999", auth_connection_id="conn-mine"))
    )
    # #4264 ③: this proves an ABSENCE (the suppressed event never renders),
    # so there is no positive effect of ITS OWN processing to wait on. A
    # sentinel event, pushed onto the SAME transport queue right after it,
    # gives one: the loop drains its single FIFO queue strictly in order
    # (one `transport.frames()` consumer, `async for`), so by the time the
    # sentinel appears in renderer.events, the suppressed event ahead of it
    # has already been fully processed — same path, same order (the #4267/
    # #4269 causal-successor shape, applied here without a new production
    # hook: renderer.events is already the real consumer both this test and
    # every sibling above rely on).
    await transport.push(EventFrame(_user_submitted_event("sentinel", "m-sentinel")))

    task = asyncio.create_task(
        run_output_loop(
            transport, renderer, own_submissions=None, own_connection_id="conn-mine",
        )
    )
    await _wait_until(lambda: "m-sentinel" in [
        e.data.get("msg_id") for e in renderer.events if e.type == "user_submitted"
    ])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "m-999" not in [
        e.data.get("msg_id") for e in renderer.events if e.type == "user_submitted"
    ], (
        "an event carrying THIS client's own connection_id must be "
        "suppressed without ever needing a matching msg_id in own_submissions"
    )


@pytest.mark.asyncio
async def test_connection_id_mismatch_still_renders_another_clients_turn() -> None:
    """Tier 2: co-vet F2 — a user_submitted event whose meta.auth_connection_id
    belongs to a DIFFERENT attached remote client still renders normally (the
    ADR-0039 multi-client invariant, now proven for the connection-identity
    correlation path specifically, mirroring the msg_id-path equivalent
    above)."""
    transport = _QueueTransport()
    renderer = _Recorder()
    await transport.push(
        EventFrame(_user_submitted_event("yes", "m-theirs", auth_connection_id="conn-theirs"))
    )

    task = asyncio.create_task(
        run_output_loop(
            transport, renderer, own_submissions=None, own_connection_id="conn-mine",
        )
    )
    # #4264 ③: wait for the loop's own observable effect instead of a sleep.
    await _wait_until(lambda: "user_submitted" in [e.type for e in renderer.events])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "user_submitted" in [e.type for e in renderer.events], (
        "another client's turn (a different connection_id) must still render"
    )


# ---------------------------------------------------------------------------
# route_input_line: the set is only populated with the msg_id the transport
# actually returns, on the branch that actually produces a user_submitted
# event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_input_line_adds_the_returned_msg_id_on_submit_branch() -> None:
    """Tier 2: an ordinary turn (no pending intervention) is delivered via
    submit_user_text, and the msg_id IT RETURNS (never the text) is added to
    own_submissions — the only value run_output_loop can later match against."""
    transport = _QueueTransport(pending_head=None, submit_msg_ids=["m-42"])
    own_submissions: "set[str]" = set()

    await route_input_line(transport, "hello world test", None, own_submissions=own_submissions)

    assert transport.submitted == ["hello world test"]
    assert own_submissions == {"m-42"}


@pytest.mark.asyncio
async def test_route_input_line_intervention_answer_does_not_add() -> None:
    """Tier 2: an intervention answer (delivered directly via
    answer_intervention_text, bypassing submit_user_text — #2690) adds
    NOTHING to own_submissions. Strip this exclusion and a later ordinary
    turn's set gains a phantom entry that no user_submitted event will ever
    arrive to consume."""
    transport = _QueueTransport(pending_head=object(), answer_ok=True)
    own_submissions: "set[str]" = set()

    await route_input_line(transport, "y", None, own_submissions=own_submissions)

    assert transport.answered == ["y"]
    assert transport.submitted == []
    assert not own_submissions, (
        "an intervention answer must never be queued as a pending self-echo"
    )


@pytest.mark.asyncio
async def test_route_input_line_empty_msg_id_is_never_added() -> None:
    """Tier 2: a transport that returns "" (no session attached, or a
    non-conforming implementation) must not pollute own_submissions with an
    empty string — an event whose own msg_id is also missing/empty (a
    malformed or foreign event) must never spuriously match."""
    transport = _QueueTransport(pending_head=None, submit_msg_ids=[""])
    own_submissions: "set[str]" = set()

    await route_input_line(transport, "hello", None, own_submissions=own_submissions)

    assert not own_submissions
