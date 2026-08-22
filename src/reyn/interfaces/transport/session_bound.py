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

from reyn.interfaces.transport.client_transport import ClientTransport

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage


class SessionBoundTransport(ClientTransport):
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

    def attach_failed(self) -> bool:
        # #5094: EXPLICITLY implemented, not inherited — a --connect remote
        # attach reaches ClientTransport.attach_failed through THIS
        # transport (the AG-UI endpoint's slash_command arm builds its
        # SlashContext over one of these, session.py:8284), so silently
        # falling through to ClientTransport's own "never happened" default
        # would be the same #5076-family defect this class exists to
        # avoid. Delegates to the session's own registry, mirroring
        # InProcessTransport's own identical derivation.
        registry = getattr(self._session, "_registry", None)
        return bool(registry.attach_failed()) if registry is not None else False

    def pending_intervention_head(self) -> "object | None":
        return self._session.interventions.head()

    async def clear_pending_command_ui(self) -> None:
        # #5094: EXPLICITLY implemented (mirrors InProcessTransport's own
        # real write, #5045) — same-thread here (session-bound, in-process
        # on the server side of a remote connection), no marshaling needed.
        self._session.set_pending_command_ui(None)

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

    async def run_slash_command(self, name: str, args: str) -> bool:
        # #5094: EXPLICITLY implemented, not inherited — mirrors
        # InProcessTransport's own real execution side (#3595 S5): the
        # handler is handed THIS transport (the send seam it already
        # writes through) plus the bound session for the reads S4
        # enumerated as residue. Un-queued by construction, same reason
        # as InProcessTransport's own copy.
        from reyn.interfaces.slash import SlashContext
        from reyn.interfaces.slash.dispatch import execute_slash_command
        return await execute_slash_command(
            SlashContext(transport=self, session=self._session), name, args,
        )

    async def request_attach(self, agent_name: str) -> bool:
        # #5094/#5096, architect ruling (issuecomment-5379623427):
        # EXPLICITLY False, not inherited -- and NOT the reach-through-
        # the-session's-own-registry implementation an earlier revision
        # of this method carried. This transport is structurally the
        # WRONG place to answer "attach a different agent" -- it is
        # send-side ONLY, bound to ONE already-attached session (see the
        # class docstring); "which agent is attached" is a REGISTRY-level
        # question, and giving this class a registry reference would
        # widen its own responsibility layer, a separate concern from
        # #5048's "registry stays off the caller's own loop" ruling but
        # the same shape of mistake. The REAL fix is #5096's own ②: the
        # CLIENT-side dispatch layer (interfaces/slash/dispatch.py's
        # maybe_dispatch_slash) now recognizes /attach and calls
        # ClientTransport.request_attach directly -- the dedicated typed
        # op AgUiTransport already implements correctly -- so a remote
        # /attach never reaches this method (or server-side slash
        # dispatch) at all.
        return False

    async def request_session_switch(self, session_id: str) -> bool:
        # #5094/#5096: EXPLICITLY False, same reasoning as request_attach
        # above -- session switching is also a registry-level operation
        # this send-side-only transport structurally cannot answer.
        return False

    async def request_artifact_list(self, *, agent: str) -> "tuple[list[dict], int]":
        # #5094: EXPLICITLY implemented, not inherited — mirrors
        # InProcessTransport's own execution side (#4494 design C /
        # #4601's own cap), reading the durable artifact-ref table
        # directly via the same reyn_state_root() this class already
        # derives above.
        from reyn.config.loader import load_config
        from reyn.data.workspace.artifact_ref import list_refs_for_agent
        reyn_root = self.reyn_state_root()
        if reyn_root is None:
            return [], 0
        project_root = reyn_root.parent
        config = load_config(project_root)
        return list_refs_for_agent(
            project_root, agent, limit=config.artifacts.remote_fallback_limit,
        )

    async def state_ready(self) -> None:
        # #5094: EXPLICITLY implemented, not inherited (even though the
        # VALUE matches ClientTransport's own default). This transport is
        # session-bound / in-process on the server's side of a remote
        # connection — no separate wire round-trip, so its own
        # status-read side-channel is already fresh the instant it is
        # asked, the same reasoning InProcessTransport's own explicit
        # override states. Written explicitly per lead-coder's #5096
        # review finding: a class that silently inherits a convenience
        # default cannot be told apart from one that forgot to answer.
        return None

    async def shutdown(self) -> None:
        await self._session.shutdown()


__all__ = ["SessionBoundTransport"]
