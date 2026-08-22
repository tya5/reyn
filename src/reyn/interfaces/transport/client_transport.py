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
Every method here is ``@abstractmethod`` — a PURE contract, no defaults
(#5076): the narrow-purpose convenience defaults 9 of these methods used to
carry live on :class:`ClientTransportStub` instead, so a DELEGATION wrapper
(one that forwards to another transport) inherits from THIS class directly
and fails to construct if it forgets one, rather than silently answering a
plausible-looking wrong value — see that class's own docstring for the
full "one base fills two roles" finding.

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

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

_logger = logging.getLogger(__name__)

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

    @abstractmethod
    def attach_failed(self) -> bool:
        """#3671 P3: whether the attach this transport is waiting on is KNOWN
        to have given up — vs. still in flight, or never attempted (both of
        which read ``False`` here, same as ``has_session()`` does for them).
        A client with ``has_session() is False`` uses this to distinguish
        "still connecting" from "gave up" (owner ruling: a client must never
        paint a genuine failure as an indefinite loading state).

        Abstract (#5076): moved OFF this contract's own convenience default
        onto :class:`ClientTransportStub` — see that class for the shared
        ``False`` body and the full rationale for why a wrapper must answer
        this itself rather than silently inheriting a value that happens to
        look plausible. ``False`` (paired with its own ``has_session()``) is
        correct for any implementation with no separate "connecting in the
        background" phase (a remote ``AgUiTransport`` attach either already
        succeeded by the time ``--connect`` returns or the connection
        attempt itself raised) — only ``InProcessTransport`` (#3671 P2's own
        background-attach path) has anything meaningful to report."""

    @abstractmethod
    def pending_intervention_head(self) -> "object | None":
        """The oldest pending intervention handle, or None — client routing input.

        Two established return shapes across today's implementations
        (#5057): a real ``UserIntervention`` (``InProcessTransport``/
        ``SessionBoundTransport``) or a bare id string
        (``AgUiTransport``'s own ``_pending_intervention_id`` — a remote
        connection has no live object to hand back). A caller that only
        needs the id (every consumer today does) should narrow through
        :func:`pending_head_id` below rather than re-deriving this
        narrowing at each call site — see its own docstring for why a
        second, independently-written copy is a real hazard, not tidiness."""

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

    @abstractmethod
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

        Abstract (#5076): the ``False``-default body moved to
        :class:`ClientTransportStub`."""

    @abstractmethod
    async def cancel_queued(self, msg_id: str) -> bool:
        """Cancel-by-id an UNDISPATCHED (queued) user message (#3300 P3
        Y-server) — a DIFFERENT intent from :meth:`cancel_inflight` (which
        targets the currently RUNNING turn), never escalated between the
        two. Returns True iff the server actually removed the item (queued);
        a no-op (already dispatched, or unknown id) returns False.

        Abstract (#5076): the no-op-``False`` default body moved to
        :class:`ClientTransportStub`; both production transports
        (``InProcessTransport``, ``AgUiTransport``) override it with the
        real op."""

    @abstractmethod
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

        Abstract (#5076): the ``False``-default body moved to
        :class:`ClientTransportStub`."""

    @abstractmethod
    async def request_session_switch(self, session_id: str) -> bool:
        """Switch the focused conversation session (the ``/session switch``
        seam); ``True`` iff it happened (#4534 PR-1).

        Same shape as :meth:`request_attach` — see that method's docstring
        for the shared rationale. Retires the ``__session_switch_request__``
        sentinel (#4534 PR-2b), reaching ``registry.attach_session``
        directly instead.

        Abstract (#5076): the ``False``-default body moved to
        :class:`ClientTransportStub`."""

    @abstractmethod
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

        Abstract (#5076): the ``([], 0)``-default body moved to
        :class:`ClientTransportStub`."""

    @abstractmethod
    async def state_ready(self) -> None:
        """Return once this transport's own STATUS/state-read side-channel
        (whatever ``ChatReadModel.snapshot()``/``intervention_head()``/etc.
        read) reflects at least one genuine update — DELIBERATELY a
        SEPARATE axis from :meth:`frames` (#5050 ③, architect ruling): a
        caller that wants to consult a status-derived read (e.g.
        ``intervention_head()`` at mount, to present a pending intervention
        this client never saw the live announce for) must not gate that on
        "has a display FRAME arrived", because state changes are announced
        to the wire only as a side effect of a frame in the current
        protocol (#4996's own docstring: "a STATE_DELTA after each frame
        when projected status changes") — a session with genuinely nothing
        else happening (measured, #5050's own finding: a fresh AG-UI
        connect carrying only STATE_SNAPSHOT + no further activity yields
        ZERO frames from :meth:`frames`, ever) would make a frame-gated
        wait hang forever, not merely late.

        NOT merged into :meth:`frames` on purpose — mixing "produce
        display frames" with "has status state landed" would be the
        exact "two axes fused into one seam" shape #5041 ① already named
        as a hazard elsewhere tonight.

        Abstract (#5076): the "return immediately" default body moved to
        :class:`ClientTransportStub` — see that class's own docstring for
        which implementations rely on it and why (``InProcessTransport``
        reads the live registry directly; ``AgUiTransport`` is the one
        implementation with a genuine "not yet" window and overrides
        this to actually wait)."""

    @abstractmethod
    async def clear_pending_command_ui(self) -> None:
        """Consume the pending command-UI request (the ``/rewind`` picker's
        own points, or any future command-UI kind) — a no-op wherever there
        is nothing to consume.

        #5045: moved here from ``ChatReadModel.clear_pending_command_ui``
        (retired — see that class's own history), which MUTATED ``Session``
        state (``s.set_pending_command_ui(None)``) despite ``ChatReadModel``
        being named and documented as read-only. True independent of any
        threading concern, but #5048's core-off-thread cutover is what makes
        it load-bearing: a read model bound to a registry that now lives on
        a WORKER thread cannot safely call a mutating method on that
        registry's ``Session`` directly — the write needs to cross the
        thread boundary the same way every OTHER write already does
        (:meth:`submit_user_text`/:meth:`answer_intervention_choice`/etc.),
        not through a read-only seam that happens to expose one exception.

        Abstract (#5076): the no-op default body moved to
        :class:`ClientTransportStub` — command-UI is INLINE-APP-LOCAL state
        (never on the wire, mirroring ``pending_command_ui()``'s own
        ``None`` for remote): ``AgUiTransport`` inherits the no-op
        unchanged. ``InProcessTransport`` overrides it to perform the real
        clear; ``ThreadedTransportProxy`` overrides it to marshal the call
        onto the worker thread that owns the ``Session``."""

    @abstractmethod
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

        Abstract (#5076): the `None`-default body moved to
        :class:`ClientTransportStub`."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Tear the session (and its registry) down — the /quit / EOF seam."""


class ClientTransportStub(ClientTransport):
    """★ ``tests/`` ONLY. No production ``ClientTransport`` implementation
    may inherit this class — see the correction below before adding a new
    one.

    A ``ClientTransport`` with the 9 narrow-purpose convenience defaults
    every full implementation used to inherit silently, now given a
    separate role from the contract itself (#5076, architect ruling, issue
    #5076 — the "one base fills two roles" finding, generalizing #5045/
    #5082/#5083/#5089's own instances of the same shape: a base default
    that LOOKS harmless lets an implementation answer wrongly by simply
    forgetting to override).

    ``ClientTransport`` itself is now a PURE contract — every method
    ``@abstractmethod``, no defaults anywhere. A subclass that forgets to
    override one of the 9 methods below fails to CONSTRUCT, the same
    completeness-by-construction guarantee the class already gave the other
    6 (always-abstract) methods. This class exists for the OTHER role a
    single base used to also carry: narrow-purpose convenience for
    ``tests/``'s own fixture doubles — a self-contained fake that never
    touches, say, ``/attach`` can inherit this and skip implementing
    ``request_attach`` at all.

    **Correction (#5096 review, lead-coder — the block this docstring is a
    condition of):** this PR's first attempt ALSO moved ``AgUiTransport``/
    ``InProcessTransport``/``SessionBoundTransport`` onto this class,
    reasoning that architect's own measurement classified them as "real
    implementations" (0 references to an inner transport) rather than
    wrappers. That reasoning was **backwards** for THIS class's purpose:
    "wrapper vs. implementation" answers a DIFFERENT question (does it
    delegate to another transport) than the one this split exists to
    answer (does forgetting a method here look plausible in PRODUCTION).
    ``SessionBoundTransport`` silently inheriting the ``request_attach``
    default (``False``, "did not happen here") is EXACTLY the owner-
    reported "attach coder-smith failed" over ``--connect`` (#5094) — a
    real implementation is not immune to this defect; it is MORE exposed
    to it, since nothing else catches a missing override once it is on the
    convenience side. All 3 production classes now stay on the pure
    ``ClientTransport`` contract and EXPLICITLY implement every one of the
    9 methods (even where the value matches this class's own default —
    written explicitly so a future reader can tell "answered, deliberately
    a no-op" from "forgot to answer"). **Only the 75 ``tests/`` fixture
    classes inherit this class** — confirmed 0 of 75 hold a reference to
    another transport (two independent methods: a per-class body read and
    a name-independent constructor-signature sweep), so none of them is a
    production-shaped risk in the first place.

    Measured (#5076 issue thread): the 2 real DELEGATION wrappers in the
    whole codebase (``ThreadedTransportProxy``, ``_ErrorWatchingTransport``)
    already override all 9 explicitly — they stay on the pure
    ``ClientTransport`` contract too, unaffected by this class, so a THIRD
    wrapper that forgets one of these 9 in the future fails to construct
    rather than silently answering wrong.

    Option ④ (``__getattr__`` auto-forwarding to an inner transport) was
    considered and rejected: ``ThreadedTransportProxy``'s job is not
    forwarding but marshaling across a thread boundary, so an
    auto-forwarded NEW method would cross unmarshaled — trading "silently
    not delegated" for "silently delegated somewhere dangerous". Falling
    at construction, not at first (mis)use, is the point.
    """

    def attach_failed(self) -> bool:
        return False

    async def run_slash_command(self, name: str, args: str) -> bool:
        return False

    async def cancel_queued(self, msg_id: str) -> bool:
        return False

    async def request_attach(self, agent_name: str) -> bool:
        return False

    async def request_session_switch(self, session_id: str) -> bool:
        return False

    async def request_artifact_list(self, *, agent: str) -> "tuple[list[dict], int]":
        return [], 0

    async def state_ready(self) -> None:
        return None

    async def clear_pending_command_ui(self) -> None:
        return None

    def reyn_state_root(self) -> "Path | None":
        return None


def pending_head_id(head: object, *, caller: str) -> "str | None":
    """Narrow :meth:`ClientTransport.pending_intervention_head`'s two
    established return shapes (#5057) down to a bare id string: a real
    ``UserIntervention`` (``InProcessTransport``/``SessionBoundTransport`` —
    #5047 axis A guarantees its ``.id`` is a genuine identity, not merely
    "the oldest"), or a bare id string already (``AgUiTransport``'s own
    ``_pending_intervention_id``).

    THE single copy of this narrowing (lead-coder's #5089 review finding,
    #5043's own discriminator: "2 or more copies — ask whether it can be
    deleted"): a second, independently-written copy of the SAME narrowing
    silently DROPPED its sibling's loud-failure half (:mod:`reyn.
    interfaces.repl.stream_client`'s original ``_pending_head_id`` warned
    on an unrecognized shape before returning ``None``; a fresh copy
    written for :mod:`reyn.interfaces.transport.threaded` returned ``None``
    silently) — the exact #4996/#5047 family: a silent ``None`` here reads
    to a caller as "no pending intervention", not "I couldn't tell", and
    ``ThreadedTransportProxy``'s own snapshot slot is exactly the kind of
    place a caller (the TUI thread) has no way to distinguish the two.

    ``caller`` names the call site in the warning (e.g. ``"stream_client"``,
    ``"ThreadedTransportProxy"``) so a future third call site's warning is
    distinguishable in logs — never reached by the two production
    transports today (this is a strict narrowing of a check that never
    fires yet, not a behavior change for either), but the id this function
    returns can feed straight into ``answer_intervention_by_id``: a caller
    passing a genuinely wrong shape deserves "no pending intervention
    recognized" (falls through to a normal turn), never a corrupted
    delivery target — never a naive ``getattr(head, "id", head)`` either,
    which would happily turn e.g. a dict-shaped value (a genuinely
    similar-looking but DIFFERENT contract lives nearby, ``RemoteReadModel.
    intervention_head()``'s own dict projection) into a garbage id string
    via ``str(...)``."""
    if head is None:
        return None
    if isinstance(head, str):
        return head
    iv_id = getattr(head, "id", None)
    if isinstance(iv_id, str) and iv_id:
        return iv_id
    _logger.warning(
        "%s: pending_intervention_head() returned an unrecognized shape "
        "(%s) -- neither a bare id string nor an object with a genuine "
        "string .id. Treating as no pending intervention rather than "
        "deriving a garbage id from it.",
        caller, type(head).__name__,
    )
    return None


__all__ = ["ClientTransport", "ClientTransportStub", "pending_head_id"]
