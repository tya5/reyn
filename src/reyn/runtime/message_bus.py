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
    (c) No in-flight async tasks remain (running_skills + running_plans all
        done for the current chain).
  Cross-chain interference: the predicate tests running_skills and
  running_plans globally, not per chain_id.  This is conservative (may wait
  slightly longer when another concurrent chain is active) but correct —
  false-positive quiescence (returning too early) is worse than false-negative
  (waiting a little longer).

P7: no skill-specific strings are embedded here.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.transport import TransportRef

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
            kind="user",
            payload={"text": message, "chain_id": chain_id},
            reply_to=ref,
            timeout=60.0,
        )
        reply_text = "\\n\\n".join(r.text for r in replies)

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
        kind: str,
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
            Inbox message kind (e.g. ``"user"``).
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

        Quiescent ≡ inbox empty AND no running skills AND no running plans.

        Cross-chain note: we do not filter by chain_id here — this is
        intentionally conservative.  See module docstring (ADR-E).
        """
        if not agent.inbox.empty():
            return False
        running_skills: dict = getattr(agent, "running_skills", {})
        if any(not t.done() for t in running_skills.values()):
            return False
        running_plans: dict = getattr(agent, "running_plans", {})
        if any(not t.done() for t in running_plans.values()):
            return False
        return True


__all__ = ["MessageBus", "new_request_id"]


def new_request_id() -> str:
    """Public alias for ``_new_request_id`` for use by transport adapters."""
    return _new_request_id()
