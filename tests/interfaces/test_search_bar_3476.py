"""#3476 ⑤ — the ctrl+n in-conversation search bar.

What these pin (all through the public surface — pressed keys, the painted
count label via ``Static.render()``, ``FlowView.current``/``display`` —
never widget internals):

- ``ctrl+n`` from the composer opens the bar and focuses its query input;
- typing searches incrementally: the cursor moves to the NEWEST match (a
  bottom-anchored conversation searches backward from now) and the ``n/M``
  count reflects the match set;
- ``Enter``/``↑`` walk toward older matches, ``↓`` back toward newer, both
  wrapping — positions verified against model order (search moves the keyboard
  CURSOR, #3493: ONE addressed position, so two rows can never both be marked);
- a match living ONLY in the lazily-held older prefix (#3476 ④) is found:
  opening search materialises the full restored history first;
- ``Escape`` closes the bar and returns focus to the composer (#3365's "Esc
  alone owns back") while KEEPING the found position on the cursor.

Real ``TextualChatApp`` + real minimal ``ClientTransport`` — no mocks."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.widgets import Input, Static

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _HYDRATE_PAGE_FRAMES
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.inline.textual_chat.search_bar import SearchBar
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
    """A real :class:`ChatReadModel` seam impl (the phase-5 suite's shape)."""

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
        return Path("/tmp/reyn_search_bar_input_history")

    def conversation_history(self, *, limit=None):
        return self._messages[-limit:] if limit is not None else list(self._messages)

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


def _count_text(app: TextualChatApp) -> str:
    """The painted match-count label, via the widget's public render."""
    return str(app.query_one("#search-count", Static).render())


def _addressed_text(app: TextualChatApp) -> "str | None":
    """The text of the ONE addressed entry — the keyboard cursor, which is what
    search moves (#3493). There is no separate selection to read."""
    from textual_flowview import FlowView

    entry = app.query_one(FlowView).current
    return None if entry is None else entry.item.text


async def _type(pilot, text: str) -> None:
    for ch in text:
        await pilot.press(ch)
    await pilot.pause()


@pytest.mark.asyncio
async def test_ctrl_n_opens_the_bar_and_focuses_the_query_input() -> None:
    """Tier 2b: ctrl+n pressed while the composer holds focus (the app's
    resting state) opens the search bar and moves focus into its input."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(SearchBar)
        assert not bar.display, "test setup: the bar is already open"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert bar.display, "ctrl+n did not open the search bar"
        assert isinstance(app.focused, Input), (
            f"focus is on {app.focused!r}, not the search input"
        )


@pytest.mark.asyncio
async def test_incremental_search_selects_the_newest_match_with_count() -> None:
    """Tier 2b: typing in the bar searches incrementally — the newest matching
    entry is selected and the count label shows its model-order position."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("alpha one", "nothing here", "alpha two", "tail"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        await pilot.press("ctrl+n")
        await _type(pilot, "alpha")
        assert _addressed_text(app) == "alpha two", (
            "incremental search did not select the newest match"
        )
        assert _count_text(app) == "2/2"


@pytest.mark.asyncio
async def test_enter_walks_older_arrows_map_spatially_and_wrap() -> None:
    """Tier 2b: Enter/↑ step toward OLDER matches, ↓ back toward newer, and
    the walk wraps at either end — verified by which entry is selected."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("match old", "filler", "match mid", "filler", "match new"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        await pilot.press("ctrl+n")
        await _type(pilot, "match")
        assert _addressed_text(app) == "match new"
        assert _count_text(app) == "3/3"

        await pilot.press("enter")
        await pilot.pause()
        assert _addressed_text(app) == "match mid", "Enter did not step older"
        assert _count_text(app) == "2/3"

        await pilot.press("up")
        await pilot.pause()
        assert _addressed_text(app) == "match old", "↑ did not step older"
        assert _count_text(app) == "1/3"

        await pilot.press("enter")
        await pilot.pause()
        assert _addressed_text(app) == "match new", (
            "the older-walk did not wrap past the oldest match"
        )

        await pilot.press("down")
        await pilot.pause()
        assert _addressed_text(app) == "match old", (
            "↓ (newer) did not wrap past the newest match"
        )


@pytest.mark.asyncio
async def test_search_finds_a_match_only_present_in_the_unpaged_prefix() -> None:
    """Tier 2b: #3476 ④/⑤ junction — a match that lives ONLY in the
    lazily-held older prefix is found, because opening search materialises
    the full restored history first."""
    log = [
        ChatMessage(role="user", content="needle-in-the-prefix"),
        ChatMessage(role="assistant", content="old reply"),
    ]
    for i in range(_HYDRATE_PAGE_FRAMES // 2):
        log.append(ChatMessage(role="user", content=f"question {i}"))
        log.append(ChatMessage(role="assistant", content=f"answer {i}"))
    app = TextualChatApp(transport=_Transport(), read_model=_HistoryReadModel(log))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not any(
            "needle-in-the-prefix" in (e.item.text or "") for e in app.conversation
        ), "test setup: the needle is already materialised"

        await pilot.press("ctrl+n")
        await _type(pilot, "needle")
        assert len(list(app.conversation)) == len(project_restored_frames(log)), (
            "search-open did not materialise the full restored history"
        )
        selected = _addressed_text(app)
        assert selected is not None and "needle-in-the-prefix" in selected, (
            f"the prefix-only match was not found (selected: {selected!r})"
        )
        assert _count_text(app) == "1/1"


@pytest.mark.asyncio
async def test_escape_closes_the_bar_keeps_the_position_and_refocuses_composer() -> None:
    """Tier 2b: Escape dismisses the bar (#3365 'Esc alone owns back') — the bar
    hides and focus returns to the composer, while the found position is KEPT on
    the cursor (#3493) so Shift+Tab back into the pane resumes from the hit
    rather than starting over. What stops showing is the MARK, not the position
    (pinned in ``test_addressed_row_rail_3490.py``)."""
    from textual_flowview import FlowView

    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="alpha"))
        await pilot.pause()
        await pilot.press("ctrl+n")
        await _type(pilot, "alpha")
        assert _addressed_text(app) == "alpha", "test setup: no active search hit"

        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one(SearchBar).display, "Escape did not hide the bar"
        assert isinstance(app.focused, Composer), (
            f"focus is on {app.focused!r}, not back on the composer"
        )
        assert app.query_one(FlowView).current is not None, (
            "the found position was thrown away instead of kept on the cursor"
        )
