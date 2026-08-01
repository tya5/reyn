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

⚠️ That guarantee covers the abstract METHOD SET, not each method's semantics,
and there is now one implementation that satisfies it without being a client:
:class:`~reyn.interfaces.transport.session_bound.SessionBoundTransport` (#3595
S4) is the SEND side only — a session builds it over itself so slash handlers,
which are client-layer code, can already depend on this seam while the dispatch
still lives in ``Session``. Its :meth:`frames` raises rather than returning an
empty stream, which is the loudest failure available to a method whose contract
is "produce frames"; it goes away with the dispatch in #3595 S5.
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
        broadcast ``user_submitted`` chat-event carries (#3300 P2a). Two
        independent things read it (#3287/#3309):

        - the LOCAL (in-process) plain-loop client, whose own terminal already
          rendered this line (``prompt_session.prompt_async``'s echo) and uses
          this id to recognise its own broadcast event and skip re-rendering
          it — never by a same-text match, which would false-positive against
          a different client's identical-text submission (co-vet finding F1
          on #3309);
        - #3300 Y-client (cancel-by-id), which needs the client to learn its
          own message id at all, regardless of transport.

        The REMOTE (AG-UI) client does NOT use this id for its own echo
        suppression — the id only becomes available once this call returns,
        racing the SSE broadcast for the same submission over the independent
        events connection (F2); it instead matches ``meta.auth_connection_id``
        on the broadcast event against its own ``connection_id`` (known
        up-front, no cross-channel race — see ``client_driver.run_chat_client``
        / ``stream_client.run_output_loop``).

        An implementation with no attached session (nothing was actually
        submitted) returns ``""`` — never ``None`` — so a caller can treat "no
        id" uniformly with a plain falsy/membership check.
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

    async def deliver_pending_answer(self, text: str) -> bool:
        """Attempt DIRECT, un-queued delivery of ``text`` as an ``/answer``
        command for a pending intervention (#3327). Returns ``True`` iff
        delivered THIS way — the caller must NOT also call
        :meth:`submit_user_text` for the same ``text``. Returns ``False``
        when ``text`` is not an ``/answer`` command, or nothing is pending —
        the caller then falls through to the ordinary queued
        :meth:`submit_user_text` path, UNCHANGED (#3300's sent-queue keeps
        gating every other submission).

        Answering a pending intervention acts on EXISTING state, not a new
        turn — queuing it behind :meth:`submit_user_text` can deadlock: with
        a turn blocked awaiting that SAME intervention, the inbox item can
        only be dequeued once the intervention resolves (chicken-and-egg,
        #3327's keyboard-only-user repro).

        NOT abstract (mirrors :meth:`cancel_queued`): added after several
        narrow-purpose ``ClientTransport`` stubs already existed across the
        test suite; the default no-op preserves their behavior unchanged.
        ``InProcessTransport`` overrides it with the real bypass.

        ``AgUiTransport`` deliberately does NOT override this — not a gap,
        verified (#3327 co-vet): the REMOTE answer path was never
        queue-gated to begin with. The plain ``--connect`` client
        (``stream_client.route_input_line``) already routes a bare (non-``/``)
        line straight to ``answer_intervention_text`` — un-queued — whenever
        ``pending_intervention_head()`` is set, no ``/answer`` needed; the
        AG-UI web surface has its own direct ``TOOL_CALL_RESULT`` POST
        (``agui/endpoint.py``'s ``_handle_answer`` → ``answer_intervention_by_id``).
        The #3327 deadlock is a Textual-chat-ONLY defect: #3299 P2
        deliberately removed the equivalent bare-text-answers-the-head
        branch from the Composer (the ``pending_intervention_head()`` read
        this class's own docstring above still mentions) as the
        no-double-input fix for that arc — leaving the Composer, alone among
        reyn's clients, with no un-queued answer path until THIS method
        added one back (scoped to ``/answer``, not bare text, to keep
        #3300's queue-everything invariant otherwise intact). AG-UI needs no
        parallel bypass because its answer path was never removed.
        """
        return False

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
