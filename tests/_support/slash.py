"""Test support for driving a slash handler directly (#3595 S4).

A slash handler is handed a :class:`~reyn.interfaces.slash.SlashContext`, not a
``Session`` — its display output goes through the client seam
(``ClientTransport.put_display``) and only its not-yet-converted session-side
reads go through ``ctx.session``. A test that calls a handler directly has to
build the same shape the production dispatch builds
(``Session._slash_context``), so this module provides it once.

:class:`RecordingTransport` is a REAL ``ClientTransport`` subclass, not a stand-in:
it implements the abstract seam and records what a client would have rendered.
The send-side methods delegate to whatever session the test supplied, so a test
that exercises one of them exercises the real call.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from reyn.interfaces.slash import SlashContext
from reyn.interfaces.transport.client_transport import ClientTransport

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class RecordingTransport(ClientTransport):
    """A real client transport that keeps what was displayed."""

    def __init__(self, session: Any = None) -> None:
        self._session = session
        self.displayed: "list[OutboxMessage]" = []

    # -- the seam under test ------------------------------------------------

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)

    # -- readers a test asserts through -------------------------------------

    def kinds(self) -> list[str]:
        """Every displayed message's kind, in order."""
        return [m.kind for m in self.displayed]

    def texts(self, kind: "str | None" = None) -> list[str]:
        """Displayed texts, optionally filtered to one kind."""
        return [m.text for m in self.displayed if kind is None or m.kind == kind]

    def system_text(self) -> str:
        """All ``system`` replies joined — the ordinary success surface."""
        return " ".join(self.texts("system"))

    def error_text(self) -> str:
        """All ``error`` replies joined — what ``reply_error`` produced."""
        return " ".join(self.texts("error"))

    # -- rest of the contract ------------------------------------------------

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def frames(self) -> "AsyncIterator[Frame]":
        raise NotImplementedError("RecordingTransport records the send side only")

    def has_session(self) -> bool:
        return self._session is not None

    def pending_intervention_head(self) -> "object | None":
        return self._session.interventions.head()

    async def submit_user_text(self, text: str) -> str:
        return await self._session.submit_user_text(text)

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        if intervention_id is not None:
            return bool(
                await self._session.answer_intervention_by_id(intervention_id, text)
            )
        return bool(await self._session.answer_oldest_intervention_text(text))

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        if intervention_id is not None:
            return bool(
                await self._session.answer_intervention_by_id(
                    intervention_id, "", choice_id_override=choice_id
                )
            )
        return bool(await self._session.answer_oldest_intervention_choice(choice_id))

    async def cancel_inflight(self) -> None:
        await self._session.cancel_inflight()

    async def shutdown(self) -> None:
        await self._session.shutdown()


def slash_ctx(
    session: Any = None, *, recorder: "list[OutboxMessage] | None" = None
) -> SlashContext:
    """The context a slash handler is handed, with a recording transport.

    Mirrors ``Session._slash_context``: the same two fields, so a handler
    driven from a test takes the same path it takes in production. Read the
    display output back through ``ctx.transport``.

    ``recorder`` lets a test that already owned a display recorder keep it: the
    transport records INTO that list rather than one of its own, so a fake
    session's existing readers keep answering and the assertions a test already
    made are the ones still being made. Pass the SAME list each call — a handler
    driven twice (a two-step confirm) must accumulate, not restart.
    """
    transport = RecordingTransport(session)
    if recorder is not None:
        transport.displayed = recorder
    return SlashContext(transport=transport, session=session)


__all__ = ["RecordingTransport", "slash_ctx"]
