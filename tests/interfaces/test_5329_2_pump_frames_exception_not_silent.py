"""Tier 2: #5329 ② — ``TextualChatApp._pump_frames`` no longer conflates a
genuine exception from its frame stream with a clean end-of-stream.

lead-coder's own real-machine measurement (issuecomment-5443600309,
``origin/main`` at ``34d66ee94``, AST): the 18 OTHER failure points inside
``_pump_frames`` are all guarded (a frame's own ingest, chrome refresh, layout
decision — each wrapped in its own ``except Exception: logger.exception(...)``
so a single bad frame never kills the pump). The ONE thing left unguarded was
the loop's own supply, ``self._transport.frames()`` itself: zero ``except``
handlers, and a ``finally: self.exit()`` that ran identically whether the
stream ended cleanly or blew up. Owner's own real-machine observation this
session — "the process completely terminates and returns to shell" — matches
this shape exactly, though matching a shape is not a root-cause claim (lead-
coder's own explicit caveat, having been wrong 3 times tonight about what
actually caused #5329's crash): the fix here does not depend on knowing why
``frames()`` raised, only that when it does, that fact must survive past the
same silent ``self.exit()`` a clean shutdown also takes.

This is the direct continuation of #5126 (``AgUiTransport._pump_sse``,
``tests/interfaces/test_5126_pump_sse_exception_not_swallowed.py``), which
fixed the PRODUCER side of this exact class of bug (``frames()`` used to
disguise a real failure as a clean end; #5126 made it RAISE the real
exception instead). #5329 ② is the next layer up: now that ``frames()``
correctly raises, THIS consumer had no handler for it at all. Mirrors that
PR's own two-test shape (a real failure case + a clean-end regression guard)
and its own witness idiom: inject the failure by raising out of a real,
minimal ``ClientTransportStub`` subclass, driven through a REAL
``TextualChatApp`` via ``app.run_test()`` (Textual's own real test harness,
the same one every other Phase-1/2/3 TUI test in this suite uses,
e.g. ``test_textual_chat_phase1_3273.py``'s ``ScriptedTransport``) — no
mocks, no duration: ``pilot.pause()`` yields control back to the event loop
exactly enough times for the worker to run, never a sleep.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import pytest
from textual.worker import WorkerFailed

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_LOGGER_NAME = "reyn.interfaces.inline.textual_chat.app"


class _InjectedPumpFailure(RuntimeError):
    """Stands in for a real transport-level failure (#5329's own motivating
    case is a 429 quota exhaustion, but what actually raises is irrelevant
    to the property under test — lead-coder's own explicit scoping: "quota
    を再現できなくても構いません。任意の例外で witness が書ければ十分")."""


class _RaisingTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` (``ClientTransportStub``
    supplies every OTHER abstract method's default) whose ``frames()``
    yields one genuine frame — proving pre-failure frames still got
    delivered, the same sanity ``test_5126``'s own witness checks — then
    raises. Mirrors that PR's ``_sse_lines_then_fail`` exactly, one layer
    up the stack."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(OutboxMessage(kind="agent", text="before the failure"))
        raise _InjectedPumpFailure("simulated transport failure")

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


class _CleanEndTransport(ClientTransportStub):
    """The regression-guard sibling: a stream that ends NORMALLY (no
    exception, just ``__end__``) — the fix must not turn this into a
    logged failure. Only a genuine exception should produce the new log
    line."""

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
async def test_a_pump_exception_is_logged_distinguishably_before_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #5329 ②'s own witness — a genuine exception from
    ``self._transport.frames()`` must leave a distinguishable, durable
    record (``reyn.log``, the same channel the 18 sibling guards in this
    method already use, and the same path ``chrome.py``'s own diagnostics
    line names to the operator inline) BEFORE the app exits — not merely
    SOME record eventually, at the exact point that distinguishes this
    from a clean shutdown.

    Strip-falsifier: reverting the new ``except Exception: logger.exception(
    ...); raise`` block around the ``async for`` (back to the bare
    ``try/finally`` with zero handlers) turns this red — no matching log
    record exists, because nothing in ``_pump_frames`` ever saw the
    exception to log it. No duration anywhere: ``pilot.pause()`` yields to
    the event loop exactly enough times for the worker to process the one
    real frame and then hit the injected raise; CI's own ``--timeout`` is
    the only ceiling.

    ``run_test``'s own pilot re-raises the worker's failure as
    ``WorkerFailed`` on context exit — this is Textual's OWN pre-existing
    behaviour (``Worker._run``'s ``exit_on_error`` path calls
    ``app._handle_exception``, docstring: "Always results in the app
    exiting"), unconditional on whether ``_pump_frames`` itself has a
    handler. It fired identically before this fix (the bare, handler-less
    ``try/finally`` never stopped the exception reaching the worker
    machinery) — the fix adds the log line, not this propagation, so
    asserting it here is not asserting the fix itself, only setting up a
    place to observe it from."""
    transport = _RaisingTransport()
    app = TextualChatApp(transport=transport)
    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        with pytest.raises(WorkerFailed):
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.pause()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    messages = [r.message for r in records]
    assert any(
        "_pump_frames' frame stream" in m and "raised" in m for m in messages
    ), (
        f"#5329 REGRESSION: a genuine _pump_frames exception should be "
        f"logged, distinguishably from a clean end-of-stream — got "
        f"{messages!r}"
    )
    # The distinguishing property is structural, not wording: this record
    # carries the real exception (``exc_info``, set by ``logger.exception``),
    # which is what makes it recoverable evidence rather than a bare "some
    # error happened" line — the same property a clean end-of-stream (next
    # test) never produces at all.
    assert any(r.exc_info is not None for r in records), (
        "the log record should carry the real exception (exc_info), not "
        "just a message — that's what makes this durably distinguishable "
        "from a clean shutdown, not merely the wording"
    )


@pytest.mark.asyncio
async def test_a_clean_end_of_stream_logs_nothing_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: regression guard, mirrors #5126's own
    ``test_a_clean_end_of_stream_still_returns_normally`` — an ORDINARY
    end-of-stream (no exception, just ``__end__``) must NOT trigger the
    new log line. Only a genuine exception should surface as one; a clean
    end is still silent, exactly as it was before this fix (the fix adds
    a signal on the exception path, not noise on the normal one).

    The deny-side assert alone (architect's finding, TESTS-READ (B)) is
    satisfied by an empty ``messages`` list for reasons that have nothing
    to do with the fix — the app failing to start, the logger name
    drifting, ``caplog`` catching nothing. The affirmative assert below
    closes that gap: it requires the SAME pump that would carry the new
    log line to have actually run and delivered its frame."""
    from textual_flowview import FlowView

    transport = _CleanEndTransport()
    app = TextualChatApp(transport=transport)
    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            delivered = [e.item.text for e in app.query_one(FlowView).entries]

    assert "a normal reply" in delivered, (
        f"the pump must have actually run and rendered the clean-end "
        f"frame — got {delivered!r}; without this, the assert below "
        f"would pass just as well on a pump that never ran at all"
    )
    messages = [r.message for r in caplog.records if r.name == _LOGGER_NAME]
    assert not any("_pump_frames' frame stream" in m for m in messages), (
        f"#5329 REGRESSION: a CLEAN end-of-stream must not produce the "
        f"exception-only log line — got {messages!r}"
    )
