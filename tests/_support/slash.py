"""Test support for driving a slash handler, and the client layer above it.

A slash handler is handed a :class:`~reyn.interfaces.slash.SlashContext`, not a
``Session`` (#3595 S4) — its display output goes through the client seam
(``ClientTransport.put_display``) and only its not-yet-converted session-side
reads go through ``ctx.session``. A test that calls a handler directly has to
build the same shape production builds, so this module provides it once.

#3595 S5 moved the DISPATCH — the step that turns typed text into a command —
out of ``Session`` and into the shared client layer
(:func:`reyn.interfaces.slash.dispatch.maybe_dispatch_slash`). A test that used
to drive ``Session._maybe_handle_slash`` drives that instead, over one of the two
transports here: :func:`local_transport` for the production-routing claim (a real
``InProcessTransport``, exactly what a local CUI/TUI attach holds), or
:class:`RecordingTransport` when the claim is about what was displayed.

:class:`RecordingTransport` is a REAL ``ClientTransport`` subclass, not a stand-in:
it implements the abstract seam and records what a client would have rendered.
The send-side methods delegate to whatever session the test supplied, so a test
that exercises one of them exercises the real call — including
``run_slash_command``, which runs the command through the same
``execute_slash_command`` the production transports use.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, AsyncIterator

from reyn.interfaces.slash import SlashContext
from reyn.interfaces.slash.dispatch import execute_slash_command
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class RecordingTransport(ClientTransport):
    """A real client transport that keeps what was displayed."""

    def __init__(self, session: Any = None) -> None:
        self._session = session
        self.displayed: "list[OutboxMessage]" = []
        # #4534 PR-2: what request_attach/request_session_switch were CALLED
        # WITH — a slash-handler unit test's own claim is "the handler asked
        # the transport to do X", not "attaching actually works end-to-end"
        # (that integration claim has its own real-registry coverage,
        # tests/interfaces/test_4534_pr1_request_attach_switch.py). Recording
        # here mirrors how ``displayed`` already records ``put_display`` calls
        # rather than driving a real registry through a test's own hand-built
        # fake.
        self.attach_requests: "list[str]" = []
        self.session_switch_requests: "list[str]" = []

    # -- the seam under test ------------------------------------------------

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)

    async def request_attach(self, agent_name: str) -> bool:
        self.attach_requests.append(agent_name)
        return True

    async def request_session_switch(self, session_id: str) -> bool:
        self.session_switch_requests.append(session_id)
        return True

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

    def reyn_state_root(self) -> "object | None":
        # Mirrors InProcessTransport/SessionBoundTransport (#3721): None
        # when there's no session, exactly the "unresolvable" shape a
        # genuinely remote transport reports.
        if self._session is None:
            return None
        return self._session.workspace_dir.parent.parent

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

    async def cancel_inflight(self) -> str:
        return await self._session.cancel_inflight()

    async def run_slash_command(self, name: str, args: str) -> bool:
        # The SAME one line ``InProcessTransport.run_slash_command`` runs — the
        # handler is handed this transport and the test's session, so a test
        # driving the client layer exercises the real executor rather than a
        # re-implementation of it.
        if self._session is None:
            return False
        return await execute_slash_command(
            SlashContext(transport=self, session=self._session), name, args,
        )

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


def local_transport(session: Any) -> "tuple[InProcessTransport, asyncio.Queue]":
    """The real local ``ClientTransport`` over a single-session registry, plus
    the display queue it writes to.

    The SAME class a local ``reyn chat`` attach holds, so a test driving
    ``maybe_dispatch_slash`` through it takes the production path end to end:
    client interpretation → ``run_slash_command`` → the handler. Display lands on
    the registry's ``repl_outbox``, which is where a local client's own display
    (user echo, ``/copy`` result) has always landed and where the transport's
    frame pump reads it from. The queue is returned rather than dug out of the
    transport so a test never reaches into it.
    """
    repl_outbox: asyncio.Queue = asyncio.Queue()
    transport = InProcessTransport(
        SimpleNamespace(
            attached_session=lambda: session,
            repl_outbox=repl_outbox,
        ),
        intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )
    return transport, repl_outbox


def drain_display(queue: "asyncio.Queue") -> "list[OutboxMessage]":
    """Everything the client display received, in order."""
    out: "list[OutboxMessage]" = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


__all__ = [
    "RecordingTransport", "slash_ctx", "local_transport", "drain_display",
]
