"""Tier 2: #5329 A — ``_pump_frames``'s supply ending (``__end__``, a
genuine exception, or a task cancel) no longer tears the whole app down via
an unconditional ``finally: self.exit()``. Only an explicit operator
``/quit`` exits now.

Owner's own real-machine report this session: quota exhaustion (a 429) made
the TUI vanish — "process completely terminates and returns to shell" —
with no error shown. architect's #5329 design (issuecomment-5451903...,
"無音終了の機構を行で特定しました"): 4 ways ``_transport.frames()`` can stop
supplying frames ALL funneled into the same unconditional ``finally:
self.exit()``, conflating "the session ended" (``__end__``, from
``Session.run()`` completing for ANY reason, including a quota-exhaustion
fail-stop) with "the operator asked to leave" (``/quit``). Before this fix,
those were indistinguishable and BOTH exited the process.

architect's witness table (issue #5329, design comment):
    #1  transport emits __end__         -> app stays alive, reason readable
    #2  strip A's branching             -> #1 goes red
    #3  /quit                           -> still exits (positive control)
This file covers #1 and #3 (both permanent regression guards). #2 was
verified by hand this session (reverting ``_pump_frames``'s
``try/except/else`` back to a bare ``try/finally: self.exit()`` turns the
first test below red — the app exits on ``__end__`` again) and is not
re-encoded as a permanent test (a strip-falsifier is a verification step
performed during review, not a standing assertion against a mechanism this
file's own test already pins from the other direction).

Same real-harness idiom as ``test_5329_2_pump_frames_exception_not_silent.py``
(this issue's own sibling file) and ``test_5126_pump_sse_exception_not_
swallowed.py``: a real, minimal ``ClientTransportStub`` subclass, driven
through a REAL ``TextualChatApp`` via ``app.run_test()`` — no mocks, no
duration.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_LOGGER_NAME = "reyn.interfaces.inline.textual_chat.app"


class _CleanEndTransport(ClientTransportStub):
    """A stream that ends NORMALLY via the ``__end__`` sentinel — the
    real shape ``Session.run()`` completing (for ANY reason: idle
    shutdown, a quota-exhaustion fail-stop, whatever) produces on the
    wire. Mirrors ``test_5329_2``'s own ``_CleanEndTransport`` (same
    file couldn't import it directly — module-private by convention in
    that file, and this file's own docstring differs)."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(OutboxMessage(kind="agent", text="a normal reply"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    async def submit_user_text(self, text: str) -> "str | None":  # pragma: no cover - trivial
        return None

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover - trivial
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover - trivial
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover - trivial
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.mark.asyncio
async def test_supply_ending_normally_does_not_exit_the_app(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #5329 A witness #1 — a stream that ends via ``__end__`` must
    leave the app RUNNING, with a readable reason in the log.

    Strip-falsifier (verified by hand, not re-encoded here — see module
    docstring): reverting ``_pump_frames``'s ``try/except/else`` to a bare
    ``try/finally: self.exit()`` makes ``app.is_running`` False here.
    """
    transport = _CleanEndTransport()
    app = TextualChatApp(transport=transport)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            assert app.is_running, (
                "#5329 REGRESSION: the app exited when the frame supply "
                "simply ended (__end__) — only an explicit /quit should "
                "exit."
            )

    messages = [r.message for r in caplog.records if r.name == _LOGGER_NAME]
    assert any(
        "reason=session_ended" in m for m in messages
    ), (
        f"the normal-end path must leave a readable reason in the log — "
        f"got {messages!r}"
    )


@pytest.mark.asyncio
async def test_quit_still_exits_the_app() -> None:
    """Tier 2: #5329 A witness #3 (positive control) — an explicit
    operator ``/quit`` must still exit the app. Without this, #5329 A's
    "only /quit exits" change could silently regress into "nothing ever
    exits" and nothing here would catch it — this is the case that PROVES
    exit still works at all.

    Drives the real composer submit path (``on_composer_submitted``),
    not a direct ``app.exit()`` call — that handler is what #5329 A's
    docstring names as the ONLY remaining exit path, so this test must
    exercise THAT path specifically. Same real-Composer idiom as
    ``test_textual_chat_phase1_3273.py``'s own
    ``test_composer_submit_routes_to_transport``: type through the pilot,
    press Enter, no direct widget-value assignment."""
    from reyn.interfaces.inline.textual_chat import Composer

    transport = _CleanEndTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.is_running, "setup: the app must be alive before /quit"

        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("/", "q", "u", "i", "t")
        await pilot.press("enter")
        await pilot.pause()

        assert not app.is_running, (
            "#5329 REGRESSION: an explicit /quit no longer exits the app"
        )
