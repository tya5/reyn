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
    async def submit_user_text(self, text: str) -> str:
        """Submit a user turn (the ordinary new-turn path).

        Returns the server-assigned ``msg_id`` — the SAME correlation id the
        broadcast ``user_submitted`` chat-event carries (#3300 P2a) — so a
        caller that already rendered this line some other way (e.g. the
        plain PromptSession loop's own terminal echo, #3287) can recognise
        its own broadcast echo BY ID and skip re-rendering it, without a
        same-text collision false-positive against a different client's
        submission. An implementation with no attached session (nothing was
        actually submitted) returns ``""`` — never ``None`` — so a caller can
        treat "no id" uniformly with a plain falsy/membership check.
        """

    @abstractmethod
    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        """Deliver ``text`` to a pending intervention; True iff delivered.

        ``intervention_id`` (#3299 P2, R1 id-targeted delivery): when given,
        the answer is delivered to EXACTLY that intervention — never a
        head-of-queue fallback, since with ``outstanding_interventions``
        holding multiple pending entries the head is not necessarily the one
        the caller displayed. ``None`` (the default) preserves the pre-P2
        oldest-pending behavior for callers that never track an id (kept for
        API stability, not a supported new-caller pattern)."""

    @abstractmethod
    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        """Deliver a chosen ``choice_id`` to a pending intervention; True iff
        delivered. ``intervention_id`` semantics mirror
        :meth:`answer_intervention_text` (#3299 P2, R1)."""

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

    async def cancel_queued(self, msg_id: str) -> bool:
        """Cancel-by-id an UNDISPATCHED (queued) user message (#3300 P3
        Y-server) — a DIFFERENT intent from :meth:`cancel_inflight` (which
        targets the currently RUNNING turn), never escalated between the
        two. Returns True iff the server actually removed the item (queued);
        a no-op (already dispatched, or unknown id) returns False.

        NOT abstract (unlike the other send-seam methods above): this method
        was added after several narrow-purpose ``ClientTransport`` stubs
        already existed across the test suite (pre-dating #3300 P3), and
        making it abstract would force every one of them to implement a
        method irrelevant to what they test. The default no-op preserves
        their behavior unchanged; both production transports
        (``InProcessTransport``, ``AgUiTransport``) override it with the
        real op.
        """
        return False

    @abstractmethod
    async def shutdown(self) -> None:
        """Tear the session (and its registry) down — the /quit / EOF seam."""


__all__ = ["ClientTransport"]
