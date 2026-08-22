"""#3476 ④ — lazy history paging: hydrate the newest page, page older frames
in on ReachedTop.

Hydration appends only the newest ``_HYDRATE_PAGE_FRAMES`` restored frames;
the older prefix pages in a slice at a time as the user scrolls toward the
top (``FlowView.ReachedTop`` → ``insert_many(0, …)``, scroll position kept by
flowview). Owner-chosen forward infrastructure — the view-side cost of full
hydration was measured small (#3476 issue comment), so what these tests pin
is CORRECTNESS of the paging, not a performance claim:

- the initially materialised page is exactly the newest slice of the real
  projection, in order (expected values come from running the REAL
  ``project_restored_frames`` on the same log, never hand-arithmetic);
- a real scroll to the top pages the previous slice in — contiguous, in
  order — repeatedly until the full history is materialised, after which
  further top-scrolls are no-ops;
- a restored tool frame paged in lazily still gets its terminal entry state
  (the shared ``_apply_restored_state`` runs on the page-in path too);
- ``/copy`` after a long restore honors the 1=newest contract (#3486: the
  hydrate seeding used to invert the ring's eviction direction past
  ``COPY_BUFFER_MAX``) — witnessed through the real copy path, not the ring.

Real ``TextualChatApp`` + the same concrete ``ChatReadModel`` seam shape the
phase-5 restore suite drives — no mocks."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _HYDRATE_PAGE_FRAMES
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _HistoryReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (the phase-5 suite's shape):
    ``conversation_history`` serves a synthetic persisted log, so the REAL
    hydrate + projector run end to end."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, messages: "list[ChatMessage]") -> None:
        self._messages = messages

    def snapshot(self, config=None):
        return None

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    def has_command_ui_region(self) -> bool:
        return True

    def history_path(self) -> Path:
        return Path("/tmp/reyn_lazy_paging_input_history")

    def conversation_history(self, *, limit=None):
        return self._messages[-limit:] if limit is not None else list(self._messages)

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


def _turns(n_turns: int) -> "list[ChatMessage]":
    return [
        m
        for i in range(n_turns)
        for m in (
            ChatMessage(role="user", content=f"question {i}"),
            ChatMessage(role="assistant", content=f"answer {i}"),
        )
    ]


def _expected_texts(log: "list[ChatMessage]") -> "list[str]":
    """What the FULL materialised pane should hold — derived from the real
    projector, never hand-arithmetic (the projection adds e.g. the resume
    divider; recomputing its shape here would just duplicate it wrongly)."""
    return [frame.text for frame in project_restored_frames(log)]


def _texts(app: TextualChatApp) -> "list[str]":
    return [entry.item.text for entry in app.conversation]


@pytest.mark.asyncio
async def test_hydration_materialises_exactly_the_newest_page() -> None:
    """Tier 2b: with a history longer than one page, the pane opens holding
    exactly the newest ``_HYDRATE_PAGE_FRAMES`` frames — the contiguous TAIL
    of the real projection."""
    log = _turns(_HYDRATE_PAGE_FRAMES)  # 2 frames/turn + divider -> ~2 pages
    app = TextualChatApp(transport=_Transport(), read_model=_HistoryReadModel(log))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        expected_full = _expected_texts(log)
        assert len(expected_full) > _HYDRATE_PAGE_FRAMES, (
            "test setup: the history does not exceed one page"
        )
        assert _texts(app) == expected_full[-_HYDRATE_PAGE_FRAMES:], (
            "the materialised page is not the projection's newest slice"
        )


@pytest.mark.asyncio
async def test_scrolling_to_the_top_pages_older_history_in_until_exhausted() -> None:
    """Tier 2b: ReachedTop (driven by a REAL scroll to the top, not a
    hand-posted message) prepends the previous page — and repeating the
    scroll materialises the FULL projection, in order, after which further
    top-scrolls are no-ops."""
    log = _turns(_HYDRATE_PAGE_FRAMES + 10)
    app = TextualChatApp(transport=_Transport(), read_model=_HistoryReadModel(log))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        flow = app.query_one(FlowView)
        expected_full = _expected_texts(log)
        assert len(_texts(app)) < len(expected_full), (
            "test setup: nothing left to page in"
        )

        for _ in range(6):  # more rounds than pages — proves the no-op tail
            flow.scroll_to_top()
            await pilot.pause()
            await pilot.pause()
            if len(_texts(app)) == len(expected_full):
                break
            # Leave the edge so flowview re-arms the trigger for the next round.
            flow.scroll_to_bottom()
            await pilot.pause()

        assert _texts(app) == expected_full, (
            "the fully paged-in pane does not equal the projection "
            f"({len(_texts(app))} of {len(expected_full)} frames)"
        )

        flow.scroll_to_top()
        await pilot.pause()
        assert len(_texts(app)) == len(expected_full), (
            "exhausted paging was not a no-op"
        )


@pytest.mark.asyncio
async def test_lazily_paged_tool_frame_gets_its_terminal_state() -> None:
    """Tier 2b: a coalesced tool frame living in the OLDER (lazily paged)
    slice arrives with a TERMINAL entry state, not DEFAULT — the shared
    ``_apply_restored_state`` runs on the page-in path, not only on the
    initial hydrate."""
    log: "list[ChatMessage]" = [
        ChatMessage(role="user", content="old question"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{
                "id": "call_old",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }],
        ),
        ChatMessage(
            role="tool", content="Read 1 line", name="read_file",
            tool_call_id="call_old",
        ),
    ]
    log += _turns(_HYDRATE_PAGE_FRAMES // 2)  # tail keeps the tool frame unpaged
    app = TextualChatApp(transport=_Transport(), read_model=_HistoryReadModel(log))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert not any(
            e.item.kind == "tool_call_started" for e in app.conversation
        ), "test setup: the tool frame is already materialised"

        flow = app.query_one(FlowView)
        flow.scroll_to_top()
        await pilot.pause()
        await pilot.pause()

        tool_entries = [
            e for e in app.conversation if e.item.kind == "tool_call_started"
        ]
        assert tool_entries, "the tool frame never paged in"
        assert tool_entries[0].state is not EntryState.DEFAULT, (
            "the paged-in tool frame kept DEFAULT state — _apply_restored_state "
            "did not run on the page-in path"
        )


class _SentinelTransport(_Transport):
    """Emits one ``__copy_last_reply__`` display frame after mount, then stays
    open — the real public route into the copy path (no ring internals read)."""

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(OutboxMessage(kind="__copy_last_reply__", text=""))
        await asyncio.Event().wait()


@pytest.fixture()
def clipboard(tmp_path, monkeypatch):
    """A REAL ``xclip`` on ``PATH`` recording its stdin (the #3362 witness
    shape: environment arrangement, not a mock — ``copy_to_clipboard`` really
    spawns it). Atomic write (temp + rename) so a poller never reads a
    half-written sink.

    #3616 ①: ``copy_to_clipboard`` is a thin pyperclip wrapper, and
    pyperclip's own backend selection is PLATFORM-gated (only tries
    ``pbcopy`` on Darwin), so a same-named fake binary is invisible to it on
    Linux CI. Pinning the backend explicitly via pyperclip's public
    ``set_clipboard("xclip")`` — then faking ``xclip`` — is portable across
    both. See the identical fixture in
    ``test_textual_chat_copy_rewind_3362.py`` for the full rationale."""
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clipboard.txt"
    script = bindir / "xclip"
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    pyperclip.set_clipboard("xclip")

    def read():
        return sink.read_text() if sink.exists() else None

    try:
        yield read
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


@pytest.mark.asyncio
async def test_copy_after_a_long_restore_reaches_the_newest_reply(clipboard) -> None:
    """Tier 2b: #3486 — after restoring a history with MORE replies than
    ``COPY_BUFFER_MAX``, ``/copy`` (1 = newest) copies the NEWEST reply.

    Witnessed through the real public path (the ``__copy_last_reply__``
    sentinel + a real ``xclip`` stand-in), never the ring's internals.
    Falsification: pre-#3486, the hydrate seeding's ``reversed`` + ``append``
    made ``deque(maxlen)`` evict from the NEWEST side once the reply count
    crossed the cap, so this copied the (cap+1)-th-newest reply instead —
    every earlier fixture stayed under the cap and never crossed the
    inversion boundary."""
    n_turns = _HYDRATE_PAGE_FRAMES  # replies ≫ COPY_BUFFER_MAX
    log = _turns(n_turns)
    app = TextualChatApp(
        transport=_SentinelTransport(), read_model=_HistoryReadModel(log)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(60):
            await pilot.pause()
            if clipboard() is not None:
                break
        assert clipboard() == f"answer {n_turns - 1}", (
            f"/copy after a long restore copied {clipboard()!r}, not the newest "
            "reply — the ring's eviction direction inverted the 1=newest contract"
        )
