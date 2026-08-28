"""MessageBus — request/reply correlation for MCP and A2A transports.

FP-0013 Component D.

``MessageBus.request`` drives ``Session.run_one_iteration()`` from the
same task that handles the MCP / A2A request, sidestepping the anyio
stdio-starvation problem documented in FP-0013 §ADR-A.

Quiescence predicate (ADR-E):
  The bus declares the turn "complete" when ALL of the following hold:
    (a) No outbox messages for this reply_to are pending (= the RoutingLayer
        has dispatched all of them).
    (b) The agent's inbox is empty.

P7: no domain-specific strings are embedded here.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.transport import TransportRef
from reyn.runtime.turn_origin import TurnOrigin

if TYPE_CHECKING:
    from reyn.runtime.session import Session

logger = logging.getLogger(__name__)

# Maximum time to wait between pumping iterations when checking quiescence.
_QUIESCENCE_POLL_INTERVAL: float = 0.01


def _new_request_id() -> str:
    """Generate a unique request id for a new MessageBus call."""
    return uuid.uuid4().hex


class MessageBus:
    """Pump ``session.run_one_iteration()`` until quiescent for a given reply_to.

    Usage::

        bus = MessageBus()
        ref = McpRef(request_id=_new_request_id())
        replies = await bus.request(
            session,
            kind=TurnOrigin.EXTERNAL_MESSAGE,
            payload={"text": message, "chain_id": chain_id},
            reply_to=ref,
            timeout=60.0,
        )
        reply_text = "\\n\\n".join(r.text for r in replies)

    The example pairs ``McpRef`` with ``TurnOrigin.EXTERNAL_MESSAGE`` because
    that is what an MCP request IS — it showed ``kind="user"`` until #3595 step
    1b, which is the pairing that arc removed (``"user"`` claims a human typed
    the line at a first-party client, and ``Session._handle_user_message`` acts
    on that claim by handing a ``/``-prefixed line to slash dispatch). ★ A
    usage example is a template: the next author of an injection path copies
    it, so an example that names a kind untruthfully reproduces the defect
    rather than merely describing it. Pick the kind that is true of YOUR
    producer — see ``kind`` below.

    The bus puts the message on ``session.inbox`` tagged with ``reply_to``,
    then pumps ``run_one_iteration()`` until the session is quiescent *for
    this request* (inbox empty AND no in-flight tasks), collecting every
    ``OutboxMessage`` that was emitted during that window.

    Because pumping runs on the *same* asyncio task as the caller, the LLM
    await inside ``run_one_iteration`` executes synchronously on the event
    loop — no background-task starvation.

    Note: concurrent calls to the same session are serialized by the caller
    (e.g. a per-agent lock in the MCP/A2A adapter).  MessageBus itself does
    not enforce serialization so that higher-level callers can decide the
    right granularity.
    """

    async def request(
        self,
        agent: "Session",
        kind: TurnOrigin,
        payload: dict,
        reply_to: TransportRef,
        *,
        timeout: float,
    ) -> list[OutboxMessage]:
        """Put a message on ``agent.inbox`` tagged with ``reply_to``, pump
        ``agent.run_one_iteration()`` until quiescent, and return all
        OutboxMessages emitted during the turn.

        Parameters
        ----------
        agent:
            The Session to drive.
        kind:
            The ``TurnOrigin`` member ``Session._run_turn_body`` dispatches on,
            naming WHO authored ``payload["text"]``. It is a claim about origin,
            not a routing hint: ``CLIENT_INPUT`` means a human typed this at a
            first-party client, and is the ONLY member whose text Reyn
            interprets as an operator command line. Every other producer says
            what it is — see ``reyn.runtime.turn_origin`` for the full closed
            vocabulary and each member's reason. ★ Claiming ``CLIENT_INPUT``
            when a human did not type it is what made every registered slash
            command executable from model output and from a Slack message
            (#3595). The parameter used to be a free-form ``str``, so nothing
            but the caller's honesty enforced it; the type is now what does.
        payload:
            Inbox message payload dict.  The bus stamps ``reply_to`` into a
            ``_bus_reply_to`` key so handlers can propagate it to outbox
            messages (future wave; currently informational).
        reply_to:
            Transport destination.  Collected OutboxMessages are those
            emitted while quiescence is being waited for — currently ALL
            outbox messages are collected regardless of their own reply_to
            field, because outbox stamping is not yet universal.  This is
            safe for the migration: MCP/A2A always drive a single request at
            a time (serialized by lock), so all outbox during the turn
            belongs to this caller.
        timeout:
            Hard deadline in seconds.  If the agent is still not quiescent
            by the deadline, whatever replies accumulated so far are
            returned (partial=True semantics on the caller side).

        Returns
        -------
        list[OutboxMessage]
            All non-``__end__`` outbox messages emitted during the pumping
            window, in emission order.
        """
        # Put the message on the inbox.  We do NOT stamp the TransportRef
        # into the payload dict because _put_inbox serializes payload to
        # JSON (via WAL) and TransportRef dataclasses are not JSON-serializable.
        # reply_to is purely a runtime bus correlation handle.
        await agent._put_inbox(kind, payload)  # noqa: SLF001

        collected: list[OutboxMessage] = []
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            # Drain all currently available outbox messages before pumping.
            self._drain_outbox(agent, collected)

            # #5214: checked BEFORE quiescence — a completed session's
            # inbox is never consumed again (nothing calls
            # run_one_iteration() on it below), so it can never become
            # quiescent on its own; without this check the loop would
            # silently poll-sleep until `timeout` instead of stopping
            # immediately with a real reason. Real-machine observed: a
            # completed session kept getting pumped for 4h20m because
            # nothing here ever asked whether run() had already exited
            # (only inbox.empty() was checked) — see Session.run_
            # completed's own docstring for why run_one_iteration()
            # itself cannot refuse this on its own.
            if agent.run_completed:
                pending = agent.inbox.qsize()
                if pending:
                    logger.warning(
                        "MessageBus.request: agent %r's session has "
                        "already completed (run() exited) — refusing to "
                        "pump further; %d inbox message(s) enqueued for "
                        "this call will NOT be consumed",
                        getattr(agent, "agent_name", "?"), pending,
                    )
                self._drain_outbox(agent, collected)
                break

            if self._is_quiescent(agent):
                # One final drain to catch any messages emitted just before
                # the quiescence check.
                self._drain_outbox(agent, collected)
                break

            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "MessageBus.request: timeout after %.1fs for %s/%s",
                    timeout, kind, type(reply_to).__name__,
                )
                self._drain_outbox(agent, collected)
                break

            # Pump one iteration if inbox has work; otherwise yield briefly.
            if not agent.inbox.empty():
                await agent.run_one_iteration()
            else:
                await asyncio.sleep(_QUIESCENCE_POLL_INTERVAL)

        return collected

    @staticmethod
    def _drain_outbox(agent: "Session", collected: list[OutboxMessage]) -> None:
        """Non-blocking drain of all currently queued outbox messages."""
        while True:
            try:
                msg = agent.outbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if msg.kind == "__end__":
                # __end__ is a session-lifetime control signal; callers of
                # MessageBus should not see it (the session is still running).
                continue
            collected.append(msg)

    @staticmethod
    def _is_quiescent(agent: "Session") -> bool:
        """Return True when the agent has no pending work for the current call.

        Quiescent ≡ inbox empty AND no running actions AND no running plans.

        Cross-chain note: we do not filter by chain_id here — this is
        intentionally conservative.  See module docstring (ADR-E).

        Proposal 0067 P1' (#3978): ``current_task`` closes exactly the gap
        this docstring's own promise ("AND no running actions AND no
        running plans") never checked — a top-level ``dispatch_kind="async"``
        delegation (e.g. ``delegate_to_agent``) used to end the turn with the
        inbox empty, so this returned True while the peer's real answer was
        still outstanding; the caller (this class's own ``request``) would
        then hand back the "delegated" ack as if it were final. See
        ``inter_agent_messaging.py``'s ``handle_agent_response`` for where
        the marker is cleared once the real answer settles.
        """
        if not agent.inbox.empty():
            return False
        if agent.current_task is not None:
            return False
        return True


__all__ = ["MessageBus", "new_request_id"]


def new_request_id() -> str:
    """Public alias for ``_new_request_id`` for use by transport adapters."""
    return _new_request_id()
