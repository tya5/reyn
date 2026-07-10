"""``ClientTransport`` — the single seam between a chat client and its session.

ADR-0039 P1 unifies the inline CUI's two direct render paths behind ONE
transport seam so a local run exercises the same client path a remote client
(P2, AG-UI / SSE) will. A ``ClientTransport`` presents the client with:

- a unified, ordered, tagged frame stream (:meth:`frames`) merging the display
  outbox and the renderer-relevant chat-event subset (see
  :mod:`reyn.interfaces.transport.frames`); and
- a send seam (:meth:`submit_user_text`, :meth:`answer_intervention_text`,
  :meth:`answer_intervention_choice`, :meth:`put_display`,
  :meth:`cancel_inflight`, :meth:`shutdown`) that wraps today's dispatch so the
  client never touches ``Session`` / ``Workspace`` / tools directly.

That last property is the **single-writer contract**: the client (renderer +
input handling) writes to the world ONLY through the transport, which is what
makes the future remote client single-writer-safe for free. The in-process
implementation composes the existing forwarder + chat-event subscription behind
this seam; a wire implementation (P2) is a second transport, not a second
client codepath.

This is an abstract base (not a bare Protocol) so a partial implementation
fails at construction rather than silently at first use — the #1402
completeness-by-construction discipline the ``PresentationConsumer`` seam uses.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class ClientTransport(ABC):
    """The client's sole seam to its session: a tagged frame stream + a send side."""

    @abstractmethod
    def start(self) -> None:
        """Begin producing frames (wire up the display + chat-event sources)."""

    @abstractmethod
    def close(self) -> None:
        """Stop producing frames and release the underlying subscriptions."""

    @abstractmethod
    def frames(self) -> "AsyncIterator[Frame]":
        """Yield the unified, ordered, tagged frame stream (display + event)."""

    @abstractmethod
    async def submit_user_text(self, text: str) -> None:
        """Submit a user turn (the ordinary new-turn path)."""

    @abstractmethod
    async def answer_intervention_text(self, text: str) -> bool:
        """Deliver ``text`` to the oldest pending intervention; True iff delivered."""

    @abstractmethod
    async def answer_intervention_choice(self, choice_id: str) -> bool:
        """Deliver a chosen ``choice_id`` to the oldest intervention; True iff delivered."""

    @abstractmethod
    def has_session(self) -> bool:
        """Whether a session is currently attached (client input guard)."""

    @abstractmethod
    def pending_intervention_head(self) -> "object | None":
        """The oldest pending intervention handle, or None — client routing input."""

    @abstractmethod
    def put_display(self, msg: "OutboxMessage") -> None:
        """Inject a client-authored display message (user echo, /copy result, …)
        into the display stream, in order with the session's own output."""

    @abstractmethod
    async def cancel_inflight(self) -> None:
        """Cooperatively cancel the in-flight turn (ctrl-c seam)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Tear the session (and its registry) down — the /quit / EOF seam."""


__all__ = ["ClientTransport"]
