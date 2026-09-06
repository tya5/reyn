"""Tier 2: #5886 ruling ⑥ — the seq gate's rejection log is DISCRIMINATING.

A rejection whose ``meta.client_ref`` names a row THIS client is still
waiting on cannot be "already reflected": the client minted that id moments
ago for a submission it has not yet seen echoed. The only way the gate says
so is a baseline that is wrong — the fingerprint of #5886's own defect — so
it WARNS, naming the id. Every other rejection (another client's genuinely
stale delta) is the gate doing its job and stays ``debug``.

Both halves are tested, because either alone is satisfiable by the wrong
build: a build that warned on EVERY rejection passes the first test and
turns the log into noise; a build that warned on none passes the second and
keeps the failure invisible — the shape #5886 was reported in.

Driven through a real mounted ``TextualChatApp`` on the SAME two seam
implementations ``test_3338_tui_status_chrome_liveness.py`` established: a
real ``ChatReadModel`` subclass whose ``snapshot()`` the test controls (so
"the read model already carries queue_seq 5" is a fact the seed reads, not a
private poke), and a real minimal ``ClientTransport`` fed one frame at a
time. The verdict is read off the logger, the barrier is the rejection's own
log record — a definite signal in BOTH tests, since the gate rejects in
both; only the LEVEL differs, which is exactly the property under test.
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

_TEXT = "a submission the gate will reject"
_LOGGER = "reyn.interfaces.inline.textual_chat.app"


class _SeededReadModel(ChatReadModel):
    """A real ``ChatReadModel`` seam impl (the ``_MutableSnapshotReadModel``
    shape from ``test_3338``) — the surface the app needs to mount. Its
    ``snapshot()`` deliberately reports ``queue_seq 0``: #5895 moved the
    seed's source onto the frame, so if the seed still consulted this read
    model the baseline would be 0, the stale seq-1 delta below would be
    ADMITTED, and both tests would fail on "no rejection" — that is the
    strip."""

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
    """``seq 1`` against a baseline of 5 — rejected by the gate either way;
    only WHOSE submission it is should change what gets logged."""
    return EventFrame(Event(type="user_submitted", data={
        "msg_id": "m1", "chain_id": "c1", "text": _TEXT, "seq": 1,
        "meta": {"client_ref": client_ref},
    }))


def _rejections(caplog) -> "list":
    return [r for r in caplog.records if "gate rejected" in r.getMessage()]


async def _drive(app: TextualChatApp, transport: _FrameTransport, client_ref: str, pilot, caplog) -> None:
    # #5895: the frame CARRIES the baseline — queue_seq 5 rides here.
    await transport.push(StatusApplied(
        kind="snapshot",
        snapshot=QueueSnapshot(queue=(), turn_active=False, queue_seq=5),
    ))
    await transport.push(_stale_user_submitted(client_ref))
    while not _rejections(caplog):
        await pilot.pause()


@pytest.mark.asyncio
async def test_rejecting_this_clients_own_pending_submission_warns(caplog) -> None:
    """Tier 2: the fingerprint. The rejected delta's ``client_ref`` names a
    row THIS client staged and is still waiting on → WARNING, naming it."""
    caplog.set_level("DEBUG", logger=_LOGGER)
    transport = _FrameTransport()
    app = TextualChatApp(transport=transport, read_model=_SeededReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        # Staged exactly the way the composer stages one — the widget's
        # own public API, no private field.
        app.query_one(SentQueue).show_item("local:mine", _TEXT, sending=True)
        await _drive(app, transport, "local:mine", pilot, caplog)

        (record,) = _rejections(caplog)
        assert record.levelname == "WARNING", (record.levelname, record.getMessage())
        assert "local:mine" in record.getMessage()
        assert "baseline is wrong" in record.getMessage()


@pytest.mark.asyncio
async def test_rejecting_someone_elses_stale_delta_stays_debug(caplog) -> None:
    """Tier 2: the control that keeps the log discriminating. The SAME
    rejection, but the ``client_ref`` is nobody's pending row here (another
    client's genuinely stale delta) → the gate is doing its job → ``debug``,
    never a warning."""
    caplog.set_level("DEBUG", logger=_LOGGER)
    transport = _FrameTransport()
    app = TextualChatApp(transport=transport, read_model=_SeededReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        await _drive(app, transport, "local:someone-else", pilot, caplog)

        (record,) = _rejections(caplog)
        assert record.levelname == "DEBUG", (record.levelname, record.getMessage())
        assert "baseline is wrong" not in record.getMessage()
