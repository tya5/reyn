"""Tier 2: #5989 ruling ② supersedes #5886 ruling ⑥ here — same fingerprint
(a rejection whose ``client_ref`` names THIS client's own still-pending row),
different resolution. #5886 made that case WARN, loudly naming the wrong
baseline, but still rejected it — the row stayed stuck, only the log got
louder. #5989's own owner-hit measured that this is not enough: the seq-gate
should answer "is this echo mine" by IDENTITY, not by seq order, so an
identity-confirmed echo now PROMOTES despite losing a seq race (see
``RemoteQueueView.apply_user_submitted``'s own docstring for the full
mechanism). What #5886 protected — the log staying discriminating, not
turning every rejection into noise — still matters, restated for what a
rejection means NOW that identity is handled upstream: every rejection
reaching this branch is, by construction, NOT this client's own pending row
(#5989 ③, replacing #5886's WARNING/DEBUG split): it is a genuine stale or
replayed delta, and it is now ALWAYS visible (``logger.warning``, never
``debug``) — the 61-commit #5989 bisection needed exactly this trace and did
not have it.

Both halves are tested, because either alone is satisfiable by the wrong
build: a build that still REJECTS the client's own pending row passes the
second test's shape but fails the first (no promotion); a build that warns
on nothing (silent rejection restored) passes the first but fails the
second.

Driven through a real mounted ``TextualChatApp`` on the SAME two seam
implementations ``test_3338_tui_status_chrome_liveness.py`` established: a
real ``ChatReadModel`` subclass whose ``snapshot()`` the test controls (so
"the read model already carries queue_seq 5" is a fact the seed reads, not a
private poke), and a real minimal ``ClientTransport`` fed one frame at a
time.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame, QueueSnapshot, StatusApplied
from reyn.schemas.models import Event

_TEXT = "a submission the gate will now promote"
_LOGGER = "reyn.interfaces.inline.textual_chat.app"


class _SeededReadModel(ChatReadModel):
    """A real ``ChatReadModel`` seam impl (the ``_MutableSnapshotReadModel``
    shape from ``test_3338``) — the surface the app needs to mount. Its
    ``snapshot()`` deliberately reports ``queue_seq 0``: #5895 moved the
    seed's source onto the frame, so if the seed still consulted this read
    model the baseline would be 0, the stale seq-1 delta below would be
    ADMITTED regardless of identity, and both tests would fail to reach the
    gate this file actually exercises — that is the strip."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return {"queue": [], "turn_active": False, "queue_seq": 0}

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_5886_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class _FrameTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport`` fed one item at a time — the
    ``_EventOnlyTransport`` shape from ``test_3338``, widened to push a
    ``StatusApplied`` too."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push(self, item: object) -> None:
        await self._queue.put(item)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return ""

    async def run_slash_command(self, name: str, args: str) -> bool:
        return True

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


def _stale_user_submitted(client_ref: str) -> EventFrame:
    """``seq 1`` against a baseline of 5 — a losing seq race either way;
    only WHOSE submission it is decides whether the gate now promotes it
    (this client's own pending row, #5989 ②) or rejects it (anyone else's,
    a genuine stale/replayed delta, #5989 ③)."""
    return EventFrame(Event(type="user_submitted", data={
        "msg_id": "m1", "chain_id": "c1", "text": _TEXT, "seq": 1,
        "meta": {"client_ref": client_ref},
    }))


def _rejections(caplog) -> "list":
    return [r for r in caplog.records if "gate rejected" in r.getMessage()]


async def _seed_and_push(transport: _FrameTransport, client_ref: str) -> None:
    # #5895: the frame CARRIES the baseline — queue_seq 5 rides here.
    await transport.push(StatusApplied(
        kind="snapshot",
        snapshot=QueueSnapshot(queue=(), turn_active=False, queue_seq=5),
    ))
    await transport.push(_stale_user_submitted(client_ref))


@pytest.mark.asyncio
async def test_own_pending_submission_promotes_despite_losing_the_seq_race(caplog) -> None:
    """Tier 2: #5989 ②, the fix itself. The delta's ``client_ref`` names a
    row THIS client staged and is still waiting on — the gate must PROMOTE
    it (the row leaves "SENDING") despite its seq losing the race against
    the baseline, and must log NO rejection at all for it."""
    caplog.set_level("DEBUG", logger=_LOGGER)
    transport = _FrameTransport()
    app = TextualChatApp(transport=transport, read_model=_SeededReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        # Staged exactly the way the composer stages one — the widget's
        # own public API, no private field.
        sent_queue = app.query_one(SentQueue)
        sent_queue.show_item("local:mine", _TEXT, sending=True)
        await _seed_and_push(transport, "local:mine")

        while sent_queue.has_row("local:mine"):
            # rekey() replaces the LOCAL key with the server msg_id — the
            # row promoting is exactly this key disappearing.
            await pilot.pause()

        assert not sent_queue.has_row("local:mine"), "the local placeholder must have been promoted"
        assert sent_queue.has_row("m1"), "promoted under the server's own msg_id"
        assert _rejections(caplog) == [], "this client's own pending row must never be logged as rejected"


@pytest.mark.asyncio
async def test_someone_elses_stale_delta_is_still_rejected_and_now_warns(caplog) -> None:
    """Tier 2: the control that keeps ② from weakening replay protection —
    the SAME losing seq race, but the ``client_ref`` is nobody's pending
    row here (another client's genuinely stale delta, or a replay). The
    gate must still reject it (#5989's own accept criterion ②: "replay が
    今までどおり弾かれる"), and #5989 ③ requires that rejection be visible
    — ``WARNING``, never ``debug`` (#5886's own DEBUG/WARNING split is
    retired: every rejection reaching this branch is now, by construction,
    a genuine one)."""
    caplog.set_level("DEBUG", logger=_LOGGER)
    transport = _FrameTransport()
    app = TextualChatApp(transport=transport, read_model=_SeededReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        await _seed_and_push(transport, "local:someone-else")
        while not _rejections(caplog):
            await pilot.pause()

        (record,) = _rejections(caplog)
        assert record.levelname == "WARNING", (record.levelname, record.getMessage())
        assert app.query_one(SentQueue).item_count() == 0, "no row must have been resurrected"
