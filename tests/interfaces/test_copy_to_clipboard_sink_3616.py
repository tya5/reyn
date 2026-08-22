"""#3616①: ``TextualChatApp.copy_to_clipboard`` override routes every
Textual-originated copy through reyn's own local-tool sink (pyperclip via
:meth:`TextualChatApp._write_clipboard`) instead of the framework default's
raw OSC 52 write.

Scoped narrowly, matching the override's own docstring: this closes the sink
for ``TextArea``/``Input``'s native copy action (an ``Input`` — reyn's
``SearchBar`` query field — is used here as the real, reachable witness) and
for Textual's generic ``Screen``-level selection copy in the abstract. It
does NOT claim FlowView mouse-drag selection works end to end — that is
tracked separately (#3972, a ``textual-flowview`` integration gap this
override cannot reach around).

Real ``TextualChatApp`` + real minimal ``ClientTransport`` — no mocks.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import Input

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.search_bar import SearchBar
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.textual_chat_test_helpers import QueueTransport


class _CancelTrackingTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` that counts
    ``cancel_inflight`` calls — ``QueueTransport``'s own is a no-op, and this
    file's regression guard needs the count."""

    def __init__(self) -> None:
        self.cancel_calls = 0

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="agent", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:
        self.cancel_calls += 1

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.mark.asyncio
async def test_copy_to_clipboard_routes_through_the_local_sink_not_osc52() -> None:
    """Tier 2: calling ``App.copy_to_clipboard`` (Textual's own entry point,
    what ``Screen.action_copy_text``/``Input.action_copy``/``TextArea``'s own
    copy action all call) on ``TextualChatApp`` writes through
    ``_write_clipboard`` and emits NO OSC 52 escape sequence to the driver —
    the framework default's OSC 52 write is fully replaced, not
    dual-written (dual-writing would risk an async OSC 52 write reaching the
    terminal after pyperclip already set the clipboard correctly,
    overwriting it with the garbled result — the exact bug #3617 fixed)."""
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()

        write_calls: list[str] = []
        orig_write = app._write_clipboard

        def _spy_write(text: str) -> bool:
            write_calls.append(text)
            return orig_write(text)

        app._write_clipboard = _spy_write  # type: ignore[method-assign]

        driver_writes: list[str] = []
        # No assert on the driver's presence — a real app.run_test() pilot
        # always has one; if it didn't, .write below would raise on its own,
        # which is diagnostic enough without asserting on private state.
        driver = app._driver
        orig_driver_write = driver.write

        def _spy_driver_write(data: str) -> None:
            driver_writes.append(data)
            return orig_driver_write(data)

        app._driver.write = _spy_driver_write  # type: ignore[method-assign]

        app.copy_to_clipboard("hello clipboard")

        assert write_calls == ["hello clipboard"], (
            f"expected exactly one _write_clipboard call, got {write_calls!r}"
        )
        osc52_writes = [
            d for d in driver_writes if isinstance(d, str) and "\x1b]52" in d
        ]
        assert osc52_writes == [], (
            f"OSC 52 escape sequence was written despite the override: {osc52_writes!r}"
        )
        assert app.clipboard == "hello clipboard", (
            "the in-memory app.clipboard (used by TextArea/Input's own "
            "in-session paste-back) must still be updated"
        )


@pytest.mark.asyncio
async def test_search_bar_input_copy_action_reaches_the_local_sink() -> None:
    """Tier 2: the real, reachable witness — ``Input.action_copy`` (bound to
    ``ctrl+c,super+c`` inside Textual's own ``Input`` widget) on reyn's
    ``SearchBar`` query field calls ``app.copy_to_clipboard``, which this
    override redirects to the local sink. Uses ``super+c`` (not ``ctrl+c``):
    reyn's own app-level ``ctrl+c`` binding
    (``cancel_turn``, ``priority=True``, ``#3498``) consumes ``ctrl+c``
    before any widget sees it, on any focused widget — measured separately
    (issue #3616, pilot probe) — so ``super+c`` is the only currently
    reachable trigger for this override to matter through, from any widget."""
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")  # #3476 ⑤: opens the search bar, focuses its Input
        await pilot.pause()

        query_input = app.query_one(SearchBar).query_one(Input)
        assert query_input.has_focus, "test setup: search bar Input did not take focus"

        query_input.insert_text_at_cursor("needle")
        await pilot.pause()
        query_input.action_select_all()
        await pilot.pause()
        assert query_input.selected_text == "needle", (
            f"test setup: select-all did not select the typed text, "
            f"got {query_input.selected_text!r}"
        )

        write_calls: list[str] = []
        orig_write = app._write_clipboard

        def _spy_write(text: str) -> bool:
            write_calls.append(text)
            return orig_write(text)

        app._write_clipboard = _spy_write  # type: ignore[method-assign]

        await pilot.press("super+c")
        await pilot.pause()

        assert write_calls == ["needle"], (
            f"Input's own copy action did not reach the local sink via the "
            f"override, got {write_calls!r}"
        )


@pytest.mark.asyncio
async def test_ctrl_c_still_interrupts_the_turn_unaffected_by_the_override() -> None:
    """Tier 2: regression guard — the copy-sink override does not touch
    ``#3498``'s ``ctrl+c`` -> ``cancel_turn`` binding. With a selection
    active in the SearchBar's Input, ``ctrl+c`` still interrupts (never
    copies) — the override changes WHERE a reached copy goes, not WHETHER
    ``ctrl+c`` reaches a copy action at all."""
    transport = _CancelTrackingTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        query_input = app.query_one(SearchBar).query_one(Input)
        query_input.insert_text_at_cursor("needle")
        await pilot.pause()
        query_input.action_select_all()
        await pilot.pause()

        write_calls: list[str] = []
        orig_write = app._write_clipboard

        def _spy_write(text: str) -> bool:
            write_calls.append(text)
            return orig_write(text)

        app._write_clipboard = _spy_write  # type: ignore[method-assign]

        cancel_calls_before = transport.cancel_calls
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert write_calls == [], (
            f"ctrl+c must not reach the copy sink, got {write_calls!r}"
        )
        assert transport.cancel_calls == cancel_calls_before + 1, (
            "ctrl+c must still interrupt the turn (#3498), unaffected by the "
            "copy-sink override"
        )
