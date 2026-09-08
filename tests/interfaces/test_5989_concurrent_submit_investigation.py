"""#5989 investigation (tui-coder — a repro/finding artifact, NOT a landed
fix). Not wired into any gate; kept for the next person continuing this
investigation.

**CONFIRMED MECHANISM** (owner-hit, verbatim: "全くllmと会話できない...
pending queue に残ったまま反応ない...proxy は3回ほど200 OK"):

1. #5894 alone (its revert conflicts with later commits, so the owner's
   real machine runs main WITH #5907's FIFO reverted, i.e. #5894's own
   design running unserialized) lets ``TextualChatApp._submit`` spawn an
   INDEPENDENT ``run_worker`` per submission — two submissions' wire
   round-trips can run CONCURRENTLY (confirmed below:
   ``test_a_second_submit_starts_before_the_first_settles``).
2. If the server answers them OUT OF SUBMISSION ORDER (turn B's
   ``user_submitted`` echo arriving before turn A's — plausible whenever
   A's own LLM call is slower than B's), the sent-queue's seq-gate
   (``RemoteQueueView.apply_user_submitted``, read via
   ``TextualChatApp._handle_user_submitted_event``) treats A's LOWER-seq
   echo, arriving AFTER B's higher one, as STALE and REJECTS it — by
   design, the same protection that correctly drops a genuine replay.
3. Rejection only promotes the row in the ``if applied:`` branch — a
   rejected echo means A's sent-queue placeholder is NEVER promoted.
   Nothing else ever retries it. It stays "SENDING" forever.
4. The rejection is `logger.debug` only (#3688: deliberately silent to
   the operator, visible only to a log-reading investigation) — and the
   owner's OWN ``reyn.log`` was not writing at all (a separate, already-
   filed #5989 finding), making this doubly invisible.

This explains BOTH observed facts: the reply itself renders fine (#5894
does not touch ``turn_started``/reply frames), matching "応答は出力され
た", while the SPECIFIC submission whose echo lost the race stays stuck
in the sent-queue forever, matching "pending queue に残ったまま".

With #5907's FIFO present (unmodified ``main``) this is structurally
unreachable: submissions are fully serialized, so a SECOND submission's
own wire round-trip cannot even START until the first's finishes — two
echoes can never race for the SAME reason two submissions never overlap.
See ``test_a_second_submit_starts_before_the_first_settles``'s own
docstring for the direct confirmation (that test never reaches its own
second ``entered`` flag on unmodified ``main``).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _PerCallGatedTransport(ClientTransportStub):
    """Like ``test_4409_pre_send_indicator.py``'s ``_GatedTransport``, but
    each ``submit_user_text`` call gets its OWN gate (keyed by call index)
    instead of one shared gate for every call — required to hold call #1
    open while independently releasing call #2, the exact shape a
    concurrency test needs."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.calls: "list[tuple[str, str | None]]" = []
        self.gates: "list[asyncio.Event]" = []
        self.entered: "list[asyncio.Event]" = []

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        idx = len(self.calls)
        self.calls.append((text, client_ref))
        gate = asyncio.Event()
        entered = asyncio.Event()
        self.gates.append(gate)
        self.entered.append(entered)
        entered.set()
        await gate.wait()
        return f"m{idx + 1}"

    async def answer_intervention_text(self, text: str, *, intervention_id: "str | None" = None) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id: "str | None" = None) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover - trivial
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _user_submitted(*, msg_id: str, chain_id: str, text: str, seq: int, client_ref: str) -> Event:
    return Event(
        type="user_submitted",
        data={
            "text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq,
            "meta": {"client_ref": client_ref},
        },
    )


async def _yield_until(condition, *, ceiling: int = 2000) -> bool:
    """Bounded — this file is an investigation artifact demonstrating a
    REAL stuck-forever state, so an unbounded wait here would hang CI (or
    a local run) exactly the way the bug itself hangs the TUI. Returns
    whether the condition became true within the ceiling; a caller reads
    the return value rather than this ever raising a timeout itself."""
    for _ in range(ceiling):
        if condition():
            return True
        await asyncio.sleep(0)
    return False


@pytest.mark.asyncio
async def test_a_second_submit_starts_before_the_first_settles() -> None:
    """Confirms the concurrency window: #5894 alone (no #5907 FIFO) lets a
    second submission start its OWN wire worker while the first is still
    open on the transport. On unmodified ``main`` (#5907's FIFO present),
    ``entered[1]`` never sets — call #2 cannot even reach the transport
    until call #1's own unit finishes."""
    transport = _PerCallGatedTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)):
        sent_queue = app.query_one(SentQueue)
        sent_queue.show_item("local:1", "first", sending=True)
        await app._submit("first", local_id="local:1")
        assert await _yield_until(lambda: len(transport.entered) >= 1)
        await transport.entered[0].wait()

        sent_queue.show_item("local:2", "second", sending=True)
        await app._submit("second", local_id="local:2")
        landed = await _yield_until(lambda: len(transport.entered) >= 2)

        assert landed, (
            "the second submission's worker never reached the transport while "
            "the first was still open — #5907's FIFO (or an equivalent) is "
            "present and serializing submissions; the race below cannot occur"
        )
        assert transport.calls[0][1] == "local:1"
        assert transport.calls[1][1] == "local:2"
        assert sent_queue.item_count() == 2

        transport.gates[0].set()
        transport.gates[1].set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_out_of_order_echo_permanently_strands_the_earlier_rows_placeholder() -> None:
    """THE bug (see module docstring for the full mechanism). Two
    submissions' echoes arrive out of seq order (B's before A's, e.g. B's
    turn ran faster) — A's placeholder must never promote. This test
    demonstrates the CURRENT (broken) behavior: it asserts the row STAYS
    stuck through the bounded wait, which is the failure mode itself, not
    a red/green pass-condition on a fix. Read the final assertion's own
    message for what a fix must change."""
    transport = _PerCallGatedTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)):
        sent_queue = app.query_one(SentQueue)

        sent_queue.show_item("local:1", "first", sending=True)
        await app._submit("first", local_id="local:1")
        assert await _yield_until(lambda: len(transport.entered) >= 1)
        await transport.entered[0].wait()

        sent_queue.show_item("local:2", "second", sending=True)
        await app._submit("second", local_id="local:2")
        assert await _yield_until(lambda: len(transport.entered) >= 2), (
            "concurrency window absent on this tree — see the sibling test above"
        )
        await transport.entered[1].wait()

        # B's turn (seq=2) answers first — entirely plausible if A's own
        # LLM call is slower.
        await transport.push_event(
            _user_submitted(msg_id="m2", chain_id="c2", text="second", seq=2, client_ref="local:2")
        )
        assert await _yield_until(
            lambda: any("second" in row and "◇" not in row for row in sent_queue.rendered_texts())
        ), "row #2 must promote normally — it is the higher-seq arrival"

        # A's turn (seq=1) answers second — the gate now sees a LOWER seq
        # than one already applied and rejects it as stale.
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="first", seq=1, client_ref="local:1")
        )
        promoted = await _yield_until(
            lambda: all("◇" not in row for row in sent_queue.rendered_texts())
        )
        assert not promoted, (
            "THIS IS THE BUG: row #1 ('first') is now STUCK IN 'SENDING' "
            "FOREVER — its own genuine echo was rejected by the seq-gate as "
            "'stale' because a LATER submission's echo happened to arrive "
            "first. A fix must let a client's OWN still-pending submission "
            "promote regardless of a later submission's seq, or serialize "
            "submissions so this ordering can never invert (#5907's own "
            "approach) — this test currently asserts the BROKEN state; "
            "flip this assertion once a fix lands, matching whichever "
            "resolution the ruling picks."
        )
        transport.gates[0].set()
        transport.gates[1].set()
        await asyncio.sleep(0)
