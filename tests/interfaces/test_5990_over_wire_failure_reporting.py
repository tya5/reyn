"""Tier 2: #5990 — the 4 ``run_worker(...)`` call sites lead-coder's own
census classified (A) ("nobody would notice if this failed") in
``TextualChatApp``: a raise from the courier's own wire call was logged via
``logger.exception`` and nothing else — no UI signal, no audit-event, no
distinguishable process exit. Scope is exactly the 4 sites named in the
issue, confirmed by lead-coder after the census (comment on #5990): the
other 11 ``run_worker`` sites already reach a named channel (an
``OutboxMessage`` row, or — for 6 of them — crashing the whole app, tracked
separately as #5995) and Q2's 98 broader ``except`` sites are explicitly
out of scope for this issue.

- :meth:`_clear_pending_command_ui_over_wire` (2 call sites, same callee,
  app.py:5186/:5220 in the census)
- :meth:`_cancel_queued_over_wire` (app.py:7131)
- :meth:`_shutdown_then_exit` (app.py:7822)

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` throughout
(``ClientTransportStub`` supplies every abstract method's default; each
transport below overrides only the ONE method whose failure is under test)
— no ``unittest.mock``, matching #5329 ②'s own idiom for injecting a wire
failure. Observed via the PUBLIC surface only: ``FlowView.entries`` for the
error-row sites, ``TextualChatApp.return_code``/``is_running`` for the exit
site — never a private attribute.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.runtime.outbox import OutboxMessage


class _FailAsInstructedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` whose
    ``clear_pending_command_ui`` / ``cancel_queued`` / ``shutdown`` each
    raise ONLY when told to — a real attribute per call, never a mock, so a
    test can drive exactly one failure at a time and use the SAME
    transport's other methods (``has_session``, ``pending_intervention_head``,
    …) via ``ClientTransportStub``'s own real defaults."""

    def __init__(
        self,
        *,
        fail_clear: bool = False,
        fail_cancel: bool = False,
        fail_shutdown: bool = False,
    ) -> None:
        self._fail_clear = fail_clear
        self._fail_cancel = fail_cancel
        self._fail_shutdown = fail_shutdown
        self.clear_calls = 0
        self.cancel_calls: "list[str]" = []
        self.shutdown_calls = 0

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":  # pragma: no cover - unused here
        if False:
            yield None  # makes this a real (empty) async generator

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover
        return ""

    async def clear_pending_command_ui(self) -> None:
        self.clear_calls += 1
        if self._fail_clear:
            raise RuntimeError("simulated clear failure")

    async def cancel_queued(self, msg_id: str) -> bool:
        self.cancel_calls.append(msg_id)
        if self._fail_cancel:
            raise RuntimeError("simulated cancel failure")
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._fail_shutdown:
            raise RuntimeError("simulated shutdown failure")


def _error_texts(app: TextualChatApp) -> "list[str]":
    return [
        e.item.text
        for e in app.query_one(FlowView).entries
        if e.item.kind == "error"
    ]


# ---------------------------------------------------------------------------
# 1. _clear_pending_command_ui_over_wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_pending_command_ui_failure_draws_an_error_row() -> None:
    """Tier 2: a raise from the courier's own wire call must reach an
    ``error`` row on the flow — not merely ``logger.exception``, which
    nobody in the running app ever reads."""
    transport = _FailAsInstructedTransport(fail_clear=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._clear_pending_command_ui_over_wire()
        while transport.clear_calls == 0 or not _error_texts(app):
            await pilot.pause()
        assert transport.clear_calls == 1
        assert any("command-UI clear failed" in t for t in _error_texts(app)), (
            f"#5990 REGRESSION: a failed command-UI clear must be visible "
            f"on the flow — got {_error_texts(app)!r}"
        )


@pytest.mark.asyncio
async def test_clear_pending_command_ui_success_draws_no_error_row() -> None:
    """Tier 2: regression guard — a clean clear stays exactly as silent as
    it always was — the fix adds a signal on the failure path, not noise
    on the normal one."""
    transport = _FailAsInstructedTransport(fail_clear=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._clear_pending_command_ui_over_wire()
        while transport.clear_calls == 0:
            await pilot.pause()
        await pilot.pause()
        assert _error_texts(app) == []


# ---------------------------------------------------------------------------
# 2. _cancel_queued_over_wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queued_failure_draws_an_error_row() -> None:
    """Tier 2: a raise from ``cancel_queued`` itself (not the server's own
    legitimate "already dispatched" no-op — see the next test) must reach
    an error row: the operator pressed Enter expecting the row gone, and
    without this it silently might not be."""
    transport = _FailAsInstructedTransport(fail_cancel=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._cancel_queued_over_wire("m1")
        while not transport.cancel_calls or not _error_texts(app):
            await pilot.pause()
        assert transport.cancel_calls == ["m1"]
        assert any("cancel failed" in t for t in _error_texts(app)), (
            f"#5990 REGRESSION: a failed cancel_queued must be visible on "
            f"the flow — got {_error_texts(app)!r}"
        )


@pytest.mark.asyncio
async def test_cancel_queued_already_dispatched_noop_draws_no_error_row() -> None:
    """Tier 2: regression guard — the server's own legitimate
    ``removed=False`` answer (already dispatched, nothing to cancel) is
    not a failure — :class:`SentQueue`'s own row state already tells that
    story, and this path must stay exactly as silent as before."""

    class _AlreadyDispatchedTransport(_FailAsInstructedTransport):
        async def cancel_queued(self, msg_id: str) -> bool:
            self.cancel_calls.append(msg_id)
            return False

    transport = _AlreadyDispatchedTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._cancel_queued_over_wire("m1")
        while not transport.cancel_calls:
            await pilot.pause()
        await pilot.pause()
        assert _error_texts(app) == []


# ---------------------------------------------------------------------------
# 3. _shutdown_then_exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quit_after_shutdown_failure_exits_with_nonzero_return_code() -> None:
    """Tier 2: #5990's own headline gap — ``self.exit()`` used to run
    unconditionally after a shutdown failure, so a genuine transport-
    shutdown failure and a clean ``/quit`` looked identical to the
    operator. ``return_code`` is Textual's own PUBLIC, process-level
    signal (``App.return_code`` / ``sys.exit(app.return_code)`` in its own
    docs) — checkable by a script without opening any log."""
    transport = _FailAsInstructedTransport(fail_shutdown=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("/quit"))
        while app.is_running:
            await pilot.pause()
    assert transport.shutdown_calls == 1
    assert app.return_code == 1, (
        f"#5990 REGRESSION: a shutdown failure must exit with a nonzero "
        f"return_code, distinguishable from a clean /quit — got "
        f"{app.return_code!r}"
    )


@pytest.mark.asyncio
async def test_quit_after_clean_shutdown_exits_with_zero_return_code() -> None:
    """Tier 2: regression guard — an ordinary, successful ``/quit`` keeps
    exiting the same way it always did — ``return_code`` 0, the default
    Textual gives a plain ``self.exit()`` call with no arguments."""
    transport = _FailAsInstructedTransport(fail_shutdown=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("/quit"))
        while app.is_running:
            await pilot.pause()
    assert transport.shutdown_calls == 1
    assert app.return_code == 0, (
        f"a clean /quit must keep exiting with return_code 0 — got "
        f"{app.return_code!r}"
    )
