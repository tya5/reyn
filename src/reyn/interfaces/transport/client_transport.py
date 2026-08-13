"""``ClientTransport`` — the single seam between a chat client and its session.

ADR-0039 P1 unifies the inline CUI's two direct render paths behind ONE
transport seam so a local run exercises the same client path a remote client
(P2, AG-UI / SSE) will. A ``ClientTransport`` presents the client with:

- a unified, ordered, tagged frame stream (:meth:`frames`) merging the display
  outbox and the renderer-relevant audit-event subset (see
  :mod:`reyn.interfaces.transport.frames`); and
- a send seam (:meth:`submit_user_text`, :meth:`answer_intervention_text`,
  :meth:`answer_intervention_choice`, :meth:`put_display`,
  :meth:`cancel_inflight`, :meth:`shutdown`) that wraps today's dispatch so the
  client never touches ``Session`` / ``Workspace`` / tools directly.

That last property is the **single-writer contract**: the client (renderer +
input handling) writes to the world ONLY through the transport, which is what
makes the future remote client single-writer-safe for free. The in-process
implementation composes the existing forwarder + audit-event subscription behind
this seam; a wire implementation (P2) is a second transport, not a second
client codepath.

This is an abstract base (not a bare Protocol) so a partial implementation
fails at construction rather than silently at first use — the #1402
completeness-by-construction discipline the ``PresentationConsumer`` seam uses.

⚠️ That guarantee covers the abstract METHOD SET, not each method's semantics,
and there is one implementation that satisfies it without being a client:
:class:`~reyn.interfaces.transport.session_bound.SessionBoundTransport` (#3595
S4) is the SEND side only — a session builds it over itself so a slash handler,
which is client-layer code, depends on this seam even when the client that
asked for the command is on the far end of a wire. Its :meth:`frames` raises
rather than returning an empty stream, which is the loudest failure available to
a method whose contract is "produce frames".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class ClientTransport(ABC):
    """The client's sole seam to its session: a tagged frame stream + a send side."""

    @abstractmethod
    def start(self) -> None:
        """Begin producing frames (wire up the display + audit-event sources)."""

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
        broadcast ``user_submitted`` audit-event carries (#3300 P2a). Two
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

    def attach_failed(self) -> bool:
        """#3671 P3: whether the attach this transport is waiting on is KNOWN
        to have given up — vs. still in flight, or never attempted (both of
        which read ``False`` here, same as ``has_session()`` does for them).
        A client with ``has_session() is False`` uses this to distinguish
        "still connecting" from "gave up" (owner ruling: a client must never
        paint a genuine failure as an indefinite loading state).

        NOT abstract (mirrors :meth:`cancel_queued` / :meth:`run_slash_command`
        above): several narrow-purpose ``ClientTransport`` stubs across the
        test suite pre-date this method, and only `InProcessTransport` (#3671
        P2's own background-attach path) has anything meaningful to report —
        a remote (``AgUiTransport``) attach either already succeeded by the
        time ``--connect`` returns or the connection attempt itself raised,
        so there is no separate "connecting in the background" phase to fail
        remotely; the default ``False`` (paired with its own ``has_session()``)
        is correct there, not a placeholder."""
        return False

    @abstractmethod
    def pending_intervention_head(self) -> "object | None":
        """The oldest pending intervention handle, or None — client routing input."""

    @abstractmethod
    def put_display(self, msg: "OutboxMessage") -> None:
        """Inject a client-authored display message (user echo, /copy result, …)
        into the display stream, in order with the session's own output."""

    @abstractmethod
    async def cancel_inflight(self) -> str:
        """Cooperatively cancel the in-flight turn (ctrl-c seam).

        Returns a human-readable summary of WHAT was cancelled (#3903's
        ``/cancel`` slash command surfaces it verbatim) — never a claim of
        success unconditionally: a transport that cannot observe the real
        outcome (e.g. a fire-and-forget wire message) returns a
        best-effort/generic string rather than asserting something was
        actually stopped."""

    async def run_slash_command(self, name: str, args: str) -> bool:
        """Run the registered slash command ``name`` with ``args``; ``True`` iff
        it ran (#3595 S5).

        The seam the shared client-side slash layer
        (:func:`reyn.interfaces.slash.dispatch.maybe_dispatch_slash`) calls once
        it has turned typed text into a command. It takes a NAME already
        resolved against the process-local registry, never the raw line — the
        interpretation is the client's, the execution happens wherever the
        session is:

        - ``InProcessTransport`` runs it against the attached session directly;
        - ``AgUiTransport`` POSTs a typed ``slash_command`` payload, and the
          server's AG-UI endpoint runs it there. This is what keeps ``/model``
          working on a ``--connect`` attach without any transport ever
          re-testing ``startswith("/")`` — a client holds no ``Session``, and
          the eleven commands that still read session state could not run
          client-side at all.

        ``False`` means "not run HERE" (no attached session, a transport with
        no execution side, an unknown name on the far end); the caller surfaces
        that rather than silently dropping the line.

        Executing a slash command is un-queued by construction: a client-side
        layer has no inbox to queue it on. See the dispatch module docstring for
        why the #3327 ``/answer`` fast path this method replaces is generalized
        rather than preserved.

        NOT abstract (mirrors :meth:`cancel_queued`): several narrow-purpose
        ``ClientTransport`` stubs across the test suite pre-date it, and the
        default keeps their behavior unchanged.
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

    async def request_attach(self, agent_name: str) -> bool:
        """Attach to a different agent (the ``/attach`` seam); ``True`` iff it
        happened (#4534 PR-1).

        Retires the ``__attach_request__`` display-channel sentinel (#4534
        PR-2 — #3595 S5's own principle, "client interprets, server executes
        a named operation", applied here the same way :meth:`run_slash_command`
        already applies it for slash commands): a typed request naming the
        target directly, not a magic string smuggled through the
        outbox/display stream that ``registry._forwarder``/the AG-UI
        transport had to specially detect. This is now the only way
        ``/attach`` and ``/agent new`` reach ``registry.attach`` — nothing
        constructs the sentinel anymore.

        ``False`` means "did not happen here" (no attached session / no
        execution side / unknown agent on the far end) — mirrors
        :meth:`run_slash_command`'s own convention.

        NOT abstract (same reasoning as :meth:`run_slash_command`/
        :meth:`cancel_queued` above): several narrow-purpose
        ``ClientTransport`` stubs across the test suite pre-date this
        method; the default ``False`` preserves their behavior unchanged.
        """
        return False

    async def request_session_switch(self, session_id: str) -> bool:
        """Switch the focused conversation session (the ``/session switch``
        seam); ``True`` iff it happened (#4534 PR-1).

        Same shape as :meth:`request_attach` — see that method's docstring
        for the shared rationale. Retires the ``__session_switch_request__``
        sentinel (#4534 PR-2b), reaching ``registry.attach_session``
        directly instead.

        NOT abstract, same reasoning as :meth:`request_attach`.
        """
        return False

    async def request_artifact_list(self, *, agent: str) -> "tuple[list[dict], int]":
        """#4494 design C: the durable artifact-ref table's own entries for
        *agent* — ``([{"ref", "path"}, ...], total)``, newest-first, or
        ``([], 0)`` when there is nothing (or this transport does not
        support the query). Same "client interprets, server executes a
        named operation" shape :meth:`request_attach`/
        :meth:`request_session_switch` already establish: ``InProcessTransport``
        reads the table directly; ``AgUiTransport`` POSTs a typed request
        and the server reads its OWN copy (never the wire's stale view —
        the server always has the live, durable table this client cannot
        see any other way).

        This is the FALLBACK a caller reaches for only when its own live
        conversation-derived artifact list is empty (frame-sufficiency: a
        remote client's past turns are not on the wire; a local client
        right after a restart has the identical gap, #4584's own measured
        finding — ``restore.project_restored_frames`` has no
        "presentation" kind reconstruction). Rows from this source are
        ALWAYS ref-backed (real files only — the table never records an
        inline artifact) and carry no ``media_type``/``description`` — a
        real information loss the caller's own UI must disclose, never
        silently absorb (lead-coder's #4494 ruling).

        **#4601**: the entries are already CAPPED (newest-first) by the
        implementation before this method returns them — ``total`` is
        the full matching count before that cap, so a caller can
        disclose "newest N of M" rather than silently dropping the tail
        (the defect #4601 exists to close: this fallback's underlying
        table is append-only/persist-tier, #4584, so an uncapped read
        only ever grows).

        NOT abstract, same reasoning as :meth:`request_attach` — several
        narrow-purpose stubs across the test suite pre-date this method;
        the default ``([], 0)`` preserves their behavior unchanged."""
        return [], 0

    def reyn_state_root(self) -> "Path | None":
        """The attached session's project `.reyn` root, or None (#3721).

        A slash handler that needs to resolve a project-scoped path (the
        memory store, e.g.) is exactly the shape #3595 S4 exists to route
        through the transport rather than `SlashContext.session` directly —
        the field is declared migration residue, and reading a NEW attribute
        off it (even a public one) would grow what the ratchet in
        `tests/interfaces/test_3595_s4_slash_handler_seam.py` is closing rather than
        adding a designed operation. This is that operation.

        None means "cannot be resolved through THIS transport" — not "no
        project" and not "empty". A caller must surface that distinction
        rather than treat None the same as an empty result (#3721's own
        fix condition): for `AgUiTransport`, the project lives on the far
        end of the wire and there is no local answer to give, ever, not a
        transient failure.

        NOT abstract (mirrors :meth:`run_slash_command` / :meth:`cancel_queued`
        above): several narrow-purpose `ClientTransport` stubs across the test
        suite pre-date this method. The default `None` is correct, not a
        placeholder, for any transport with no local session to ask.
        """
        return None

    @abstractmethod
    async def shutdown(self) -> None:
        """Tear the session (and its registry) down — the /quit / EOF seam."""


__all__ = ["ClientTransport"]
