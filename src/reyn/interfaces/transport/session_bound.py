"""``SessionBoundTransport`` — a send-side :class:`ClientTransport` over one session.

Slash handlers are client-layer code: the owner's design for #3595 is that a
client interprets ``/``-prefixed text and maps it onto *published* operations,
and that ``Session`` never interprets a string. The seam a client writes through
already exists — :class:`~reyn.interfaces.transport.client_transport.ClientTransport`,
whose :meth:`~reyn.interfaces.transport.client_transport.ClientTransport.put_display`
docstring has always named the ``/copy`` result as one of its payloads.

#3595 S5 moved the dispatch into the client, and a LOCAL client now passes its
OWN transport (``InProcessTransport``). ★ This class survives for the REMOTE
case, and for a reason that belongs to the residue rather than to the dispatch:
a ``--connect`` client holds no ``Session``, so the commands that still read
session state (``SlashContext.session`` — the declared, shrinking residue) can
only run on the server's side of the wire. The AG-UI endpoint's
``slash_command`` arm runs them there, against a command NAME the client already
resolved, and this is the seam it hands them. It is deleted when
``SlashContext.session`` is — at which point every command runs client-side and
a remote client needs nothing but the registry it already has.

**Send side only.** :meth:`frames` raises: a session cannot consume its own
display stream, and a caller reaching for it is a caller that should be holding
the client's real transport instead. The four abstract send methods delegate to
the session's existing PUBLIC API — the same methods ``InProcessTransport``
calls on its attached session, so the two transports agree by construction
rather than by parallel implementation.

⚠️ ``put_display`` is the one method that does NOT delegate to a public
``Session`` method, because none exists and #3595 S4's success metric is that
none is added: publishing ``_put_outbox`` as ``put_outbox`` would ratify exactly
the encapsulation break the arc is closing. It takes the session's outbox sink
as an injected sync callable instead, which the session supplies from its own
private path (``Session._put_outbox_nowait``). Routing is therefore unchanged:
a slash reply lands on ``session.outbox``, in FIFO order with the session's own
output, exactly where ``session._put_outbox`` put it before.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator, Callable

from reyn.interfaces.transport.client_transport import ClientTransportStub

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class SessionBoundTransport(ClientTransportStub):
    """The send half of the client seam, bound to the session that dispatches."""

    def __init__(
        self,
        session: "object",
        *,
        display_sink: "Callable[[OutboxMessage], None]",
    ) -> None:
        # ``session`` is duck-typed: this module lives under ``interfaces`` and a
        # runtime import of ``Session`` here would make the transport package
        # depend on the runtime it is supposed to sit in front of.
        self._session = session
        self._display_sink = display_sink

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """No-op: this transport produces no frames, so there is nothing to wire."""

    def close(self) -> None:
        """No-op: mirror of :meth:`start`."""

    def frames(self) -> "AsyncIterator[Frame]":
        """Always raises — a session-bound transport has no frame stream.

        Fail loud rather than returning an empty iterator: an empty stream is
        indistinguishable from a session that has produced nothing yet, so a
        caller that wired this by mistake would hang instead of erroring.
        """
        raise NotImplementedError(
            "SessionBoundTransport is send-side only (#3595 S4): the display "
            "stream belongs to the client's own transport (InProcessTransport / "
            "AgUiTransport). Hold that one if you need frames."
        )

    # -- send side ----------------------------------------------------------

    def has_session(self) -> bool:
        """Always True: this transport exists only because a session built it."""
        return True

    def pending_intervention_head(self) -> "object | None":
        return self._session.interventions.head()

    def reyn_state_root(self) -> "Path | None":
        # #3721: same derivation as InProcessTransport (mirrors that class by
        # this file's own design convention — both delegate to the session's
        # existing public API rather than re-deriving anything).
        return self._session.workspace_dir.parent.parent

    async def submit_user_text(self, text: str) -> str:
        return await self._session.submit_user_text(text)

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        # #5057: the oldest-fallback closed — see InProcessTransport's own
        # mirror of this method for the full rationale. Fail-closed on no
        # id, never "guess the oldest".
        if intervention_id is None:
            return False
        return bool(
            await self._session.answer_intervention_by_id(intervention_id, text)
        )

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        if intervention_id is None:
            return False
        return bool(
            await self._session.answer_intervention_by_id(
                intervention_id, "", choice_id_override=choice_id
            )
        )

    def put_display(self, msg: "OutboxMessage") -> None:
        self._display_sink(msg)

    async def cancel_inflight(self) -> str:
        return await self._session.cancel_inflight()

    async def cancel_queued(self, msg_id: str) -> bool:
        return bool(await self._session.cancel_queued(msg_id))

    async def shutdown(self) -> None:
        await self._session.shutdown()


__all__ = ["SessionBoundTransport"]
