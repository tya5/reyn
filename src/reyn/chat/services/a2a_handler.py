"""A2AHandler — agent-to-agent messaging logic.

Extracted from ChatSession (FP-0019 Wave 2 part 2, final).  Owns the
agent-side of the A2A protocol: inbox handling for ``agent_request`` /
``agent_response``, pending-chain lifecycle, and multi-hop relay
coordination.

Transport-side routing (AgentRef → other-agent inbox) is handled by
the FP-0013 RoutingLayer via the injected ``send_request_callback`` and
``send_response_callback``.  A2AHandler is transport-agnostic: it
constructs the payload but delegates the actual put to callbacks, which
makes it compatible with both the existing session-registry transport
and the FP-0013 RoutingLayer's ``AgentRef`` handler.

Design constraints (same pattern as Wave 1/1b/2 services):
- Injected deps at construction (typed + Callable callbacks).
- No direct reference to ChatSession.
- All state mutations go through injected event_log (P6).
- No skill-specific strings (P7).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.chat.outbox import OutboxMessage

if TYPE_CHECKING:
    from reyn.chat.services.chain_manager import ChainManager, _PendingChain
    from reyn.events.events import EventLog
    from reyn.safety.limit_handler import LimitDecision

logger = logging.getLogger(__name__)


# ── Helpers (mirrors session.py module-level helpers) ───────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_NO_REPLY_MARKER_RE = re.compile(
    r"^\s*\[([^:]+):\s*could not produce a reply\s*[—\-]\s*(.+?)\s*\]\s*$",
    re.DOTALL,
)


def _is_no_reply_marker(text: str) -> bool:
    """Detect whether ``text`` is a ``_no_reply_marker(...)``-formatted failure signal."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith("[") and "could not produce a reply" in stripped


def _parse_no_reply_marker(text: str) -> tuple[str, str] | None:
    """Parse ``_no_reply_marker(...)`` text into ``(peer, reason)``."""
    m = _NO_REPLY_MARKER_RE.match(text or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _no_reply_marker(agent_name: str, reason: str) -> str:
    """Structured upstream message when this agent's router couldn't produce a reply."""
    return f"[{agent_name}: could not produce a reply — {reason}]"


def _new_chain_id() -> str:
    import uuid
    return str(uuid.uuid4())


# Localized user-facing message when a peer agent's reply signals failure (B2-H2).
_PEER_REPLY_FAILED_MSG: dict[str, str] = {
    "ja": (
        "エージェント '{peer}' から処理結果が得られませんでした"
        " (理由: {reason})。"
    ),
    "en": (
        "Could not get a result from agent '{peer}' "
        "(reason: {reason})."
    ),
}


# ── RouterCapExceeded reference ──────────────────────────────────────────────
# Imported locally to avoid circular import; this mirrors session.py's pattern.


class A2AHandler:
    """Agent-to-agent messaging service.

    Extracted from ChatSession (FP-0019 Wave 2 part 2).

    Owns:
    - ``_send_to_agent`` — depth-guarded delegation request dispatch
    - ``_send_agent_response`` — response routing back to requester
    - ``_handle_agent_request`` — inbox handler for ``agent_request``
    - ``_handle_agent_response`` — inbox handler for ``agent_response``
    - ``_resolve_pending_chain`` — multi-hop relay completion logic

    Parameters
    ----------
    event_log:
        Session-scoped :class:`~reyn.events.events.EventLog`.  All A2A
        lifecycle events are emitted here (P6).
    chain_manager:
        :class:`~reyn.chat.services.chain_manager.ChainManager` owning
        pending-chain lifecycle and timeout watchdogs.
    agent_name:
        Name of the owning agent (used in event payloads + no-reply markers).
    max_hop_depth:
        Maximum delegation hop depth (LangGraph-style guard).
    safety_extensions:
        Mutable ``dict[str, float]`` from ChatSession — shared by reference so
        extensions granted by ``handle_chat_limit_checkpoint`` are visible here.
    output_language:
        BCP-47 language code for localised user-facing error messages.  Falls
        back to ``"en"`` when the code is not in the message table.
    append_history:
        Callable ``(role, text, ts, meta) -> None``.  Appends a history entry.
    put_outbox:
        Async callable ``(OutboxMessage) -> None``.  Forwards messages to the
        session outbox.
    handle_chat_limit_checkpoint:
        Async callable for FP-0005 safety-limit checkpoint decisions.
    run_router_loop:
        Async callable ``(user_text, chain_id) -> None``.  Runs the router
        loop for one utterance.  Raises ``RouterCapExceeded`` when exhausted.
    reset_router_turn_counter:
        Sync callable ``() -> None``.  Resets the per-turn router counter at
        the top of each fresh agent-request turn.
    send_request_callback:
        Async callable ``(to, from_agent, request, depth, chain_id) -> None``.
        Session-side wrapper that validates topology + performs the actual
        ``submit_agent_request`` transport call.  A2AHandler only calls this
        after depth/self-message/existence guards pass.
    send_response_callback:
        Async callable ``(to, from_agent, response, depth, chain_id) -> None``.
        Session-side wrapper for the actual ``submit_agent_response`` transport
        call.  A2AHandler calls this after the depth-drop guard passes.
    on_chain_timeout_fire:
        Async callable ``(chain_id) -> None`` — invoked by ChainManager when a
        chain's watchdog fires.  Stays in ChatSession; A2AHandler only passes
        it as ``on_fire`` to ``chain_manager.arm_timeout``.
    """

    def __init__(
        self,
        *,
        event_log: "EventLog",
        chain_manager: "ChainManager",
        agent_name: str,
        max_hop_depth: int,
        safety_extensions: dict[str, float],
        output_language: str | None = None,
        # Callbacks
        append_history: "Callable[[str, str, str, dict], None]",
        put_outbox: "Callable[[OutboxMessage], Awaitable[None]]",
        handle_chat_limit_checkpoint: "Callable[..., Awaitable[LimitDecision]]",
        run_router_loop: "Callable[[str, str], Awaitable[None]]",
        reset_router_turn_counter: "Callable[[], None]",
        send_request_callback: "Callable[[str, str, str, int, str], Awaitable[None]]",
        send_response_callback: "Callable[[str, str, str, int, str], Awaitable[None]]",
        on_chain_timeout_fire: "Callable[[str], Awaitable[None]]",
        emit_router_cap_exhausted_fn: "Callable",  # async (exc, *, chain_id) → None
        # Delegation tracking (mutable list refs owned by ChatSession)
        get_router_loop_delegations: "Callable[[], list[dict] | None]",
        set_router_loop_delegations: "Callable[[list[dict] | None], None]",
        get_router_loop_agent_replies: "Callable[[], list[str] | None]",
        set_router_loop_agent_replies: "Callable[[list[str] | None], None]",
    ) -> None:
        self._events = event_log
        self._chains = chain_manager
        self.agent_name = agent_name
        self._max_hop_depth = max_hop_depth
        self._safety_extensions = safety_extensions
        self._output_language = output_language

        self._append_history = append_history
        self._put_outbox = put_outbox
        self._handle_chat_limit_checkpoint = handle_chat_limit_checkpoint
        self._run_router_loop = run_router_loop
        self._reset_router_turn_counter = reset_router_turn_counter
        self._send_request_callback = send_request_callback
        self._send_response_callback = send_response_callback
        self._on_chain_timeout_fire = on_chain_timeout_fire
        self._emit_router_cap_exhausted_fn = emit_router_cap_exhausted_fn

        self._get_router_loop_delegations = get_router_loop_delegations
        self._set_router_loop_delegations = set_router_loop_delegations
        self._get_router_loop_agent_replies = get_router_loop_agent_replies
        self._set_router_loop_agent_replies = set_router_loop_agent_replies

    # ── Public API (mirrors former session._<name> methods) ─────────────────

    async def send_to_agent(
        self, *, to: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Route a delegation request from this agent to ``to``.

        depth is the hop count from the original user request (user → A = 1,
        A → B = 2, ...). Refused when depth > max_hop_depth (LangGraph-style
        guard, default 3). chain_id (PR14) identifies the logical request
        thread for cross-agent trace; it propagates verbatim to the target's
        inbox payload and is recorded in history meta + events.
        """
        # FP-0005: extension granted by safety-limit checkpoint raises
        # the effective hop cap for this chain.
        effective_max_hops = int(
            self._max_hop_depth
            + self._safety_extensions.get(f"max_agent_hops:{chain_id}", 0.0)
        )
        if depth > effective_max_hops:
            # FP-0005: ask before refusing when on_limit.mode opts in.
            decision = await self._handle_chat_limit_checkpoint(
                kind=f"max_agent_hops:{chain_id}",
                prompt=(
                    f"Delegation depth {depth} exceeds max_agent_hops "
                    f"({effective_max_hops}). Allow chain {chain_id} to "
                    f"continue?"
                ),
                detail=f"to={to} depth={depth} cap={effective_max_hops}",
                extension_amount=1.0,
                run_id=chain_id,
            )
            if not decision.allow_continue:
                # FP-0004: hint at the config key the operator can raise.
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=(
                        f"agent message depth {depth} exceeds limit "
                        f"{effective_max_hops}; chain refused. "
                        f"→ Raise safety.loop.max_agent_hops to allow deeper "
                        f"delegation chains."
                    ),
                    meta={"chain_id": chain_id},
                ))
                self._events.emit(
                    "agent_message_refused",
                    reason="max_hop_depth",
                    to_agent=to, depth=depth, chain_id=chain_id,
                )
                return
            # Approved — continue. ``_safety_extensions[max_agent_hops:<chain_id>]``
            # was bumped by the checkpoint helper so re-entry on the same
            # chain at this depth would not re-prompt unless depth grows again.

        if to == self.agent_name:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r}: cannot self-message",
                meta={"chain_id": chain_id},
            ))
            return

        # Sender-side audit: this agent's history records the delegation outgoing.
        self._append_history(
            "agent", request, _now_iso(),
            {
                "source": "agent_request_outgoing",
                "to_agent": to, "depth": depth, "chain_id": chain_id,
            },
        )
        self._events.emit(
            "agent_message_sent",
            kind="agent_request",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        # Delegate the actual transport call (topology check + submit) to
        # the session-side callback.  This is the FP-0013 RoutingLayer
        # integration point: the callback can be replaced with a RoutingLayer
        # AgentRef handler without changing A2AHandler.
        await self._send_request_callback(
            to, self.agent_name, request, depth, chain_id,
        )

    async def send_agent_response(
        self, *, to: str, response: str, depth: int, chain_id: str,
    ) -> None:
        """Route a reply from this agent back to the requester ``to``.

        depth is propagated from the original request (B replying to A's
        depth-1 request stays at depth 1; A's next hop will increment).
        Empty response is still sent so chains never silently stall.
        chain_id (PR14) carries the same value the original request did so
        the requester can correlate the reply with its pending chain.
        """
        if depth > self._max_hop_depth:
            return  # silently drop — sender already gave up the chain
        self._events.emit(
            "agent_message_sent",
            kind="agent_response",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        await self._send_response_callback(
            to, self.agent_name, response, depth, chain_id,
        )

    async def handle_agent_request(self, payload: dict) -> None:
        """Process an incoming ``agent_request``.

        PR14 deferred-reply path: if the router emits ``messages_to_agents``
        (= this agent wants to consult others before answering), the reply
        to the requester is held back. A ``_PendingChain`` entry is created
        keyed by chain_id; when every delegated agent has responded, the
        router runs again with all replies in history and the synthesized
        reply_text is finally sent upstream.

        If no delegations are emitted, behavior matches PR11: send the
        router's reply_text (or empty) right back to the requester.
        """
        from reyn.chat.session import RouterCapExceeded

        from_agent = payload.get("from_agent", "")
        request = payload.get("request", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        # Receiver-side audit
        self._append_history(
            "user", request, _now_iso(),
            {
                "source": "agent_request",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        )
        self._events.emit(
            "agent_request_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        # Reset the per-turn router cap counter — an inbound agent_request
        # is a fresh top-level entry into this agent's loop.
        self._reset_router_turn_counter()

        # Arm delegation and reply capture before running RouterLoop.
        self._set_router_loop_delegations([])
        self._set_router_loop_agent_replies([])
        try:
            await self._run_router_loop(request, chain_id)
        except RouterCapExceeded as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                    f"for incoming agent_request from {from_agent!r}. "
                    f"Last reason: {exc.last_reason or '(none)'}."
                ),
                meta={"chain_id": chain_id, "from_agent": from_agent},
            ))
            # F6/F7 fix: send a structured failure marker (not "") so the
            # upstream LLM doesn't mistake silence for "in-progress" and
            # retry in a tight loop.
            await self.send_agent_response(
                to=from_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router retry budget exhausted ({exc.count}/{exc.cap})",
                ),
                depth=depth, chain_id=chain_id,
            )
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_request): {exc}",
                meta={"chain_id": chain_id},
            ))
            # F6/F7 fix: send a structured failure marker so the requester
            # chain receives a clear "no reply produced" instead of an
            # ambiguous empty string.
            await self.send_agent_response(
                to=from_agent,
                response=_no_reply_marker(
                    self.agent_name, f"router error: {exc}"
                ),
                depth=depth, chain_id=chain_id,
            )
            return
        finally:
            dispatched = list(self._get_router_loop_delegations() or [])
            agent_replies = list(self._get_router_loop_agent_replies() or [])
            self._set_router_loop_delegations(None)
            self._set_router_loop_agent_replies(None)

        if dispatched:
            # PR14 deferred path: RouterLoop called send_to_agent for one or
            # more peers. Register a pending chain so the reply is held until
            # all delegates respond. ChainManager persists via the journal +
            # arms the timeout watchdog.
            waiting_on = {d["to"] for d in dispatched}
            await self._chains.register(
                chain_id=chain_id,
                from_user=False,
                depth=depth,
                original_text=request,
                sender=from_agent,
                waiting_on=waiting_on,
                origin_agent=from_agent,
                origin_depth=depth,
            )
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        # PR11-compatible single-hop reply path. RouterLoop emitted reply_text
        # via put_outbox → captured in agent_replies. Forward upstream.
        # F6/F7 fix: when no clean text reply was captured (max_iterations,
        # empty content, async-only dispatch with no follow-up text), send
        # a structured marker rather than "" so the upstream LLM doesn't
        # interpret silence as "in-progress" and re-delegate.
        if agent_replies:
            reply_text = agent_replies[0]
        else:
            reply_text = _no_reply_marker(
                self.agent_name,
                "router completed without producing a text reply",
            )
        # Note: history was already appended by put_outbox; add routing meta.
        await self.send_agent_response(
            to=from_agent, response=reply_text, depth=depth, chain_id=chain_id,
        )

    async def handle_agent_response(self, payload: dict) -> None:
        """Process an incoming ``agent_response``.

        Two branches:
        - chain_id ∈ self._chains → multi-hop relay. Drop sender
          from waiting_on; when waiting_on becomes empty, re-invoke router
          and forward the synthesized reply (or fresh delegations) on the
          same chain. Reply goes to the chain's ``origin_agent``, NOT
          ``from_agent``.
        - chain_id ∉ self._chains → user-initiated chain (PR11
          compatibility). The router's reply_text is treated as a
          user-facing message (outbox + history); further delegations
          continue with depth+1 on the same chain_id.
        """
        from reyn.chat.session import RouterCapExceeded

        from_agent = payload.get("from_agent", "")
        response = payload.get("response", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        # B55 R-7 (2026-05-25): structural symmetry with skill / plan
        # completion injections. Wrap the peer's reply in a
        # `[task_completed] kind=agent ...` header (role=user) so the
        # SP TASK_COMPLETED rule covers agent-delegation lifecycles
        # too. Prior behaviour appended the raw reply text alone,
        # which the LLM read as a plain user message — no task
        # lifecycle anchor + no parity with skill / plan paths.
        injected_text = (
            f"[task_completed] kind=agent "
            f"from={from_agent or '<unknown>'} chain_id={chain_id}\n"
            f"reply: {response}"
        )
        self._append_history(
            "user", injected_text, _now_iso(),
            {
                "source": "agent_response",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        )
        self._events.emit(
            "agent_response_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        pending = self._chains.get(chain_id)
        if pending is not None:
            await self._resolve_pending_chain(
                pending, from_agent=from_agent, last_response=response,
            )
            return

        # User-initiated chain: PR11 path, reply goes to user.

        # B2-H2 fix: if the peer's reply is a no-reply marker, bypass the LLM
        # entirely and surface the failure deterministically. Without this,
        # gemini-2.5-flash-lite interprets the marker as a polite conversational
        # reply (e.g. "かしこまりました") and the user never learns that the peer
        # failed.
        if _is_no_reply_marker(response):
            parsed = _parse_no_reply_marker(response)
            peer = parsed[0] if parsed else from_agent or "<unknown>"
            reason = parsed[1] if parsed else "no reply produced"
            msg_template = _PEER_REPLY_FAILED_MSG.get(
                self._output_language or "en", _PEER_REPLY_FAILED_MSG["en"],
            )
            user_text = msg_template.format(peer=peer, reason=reason)
            await self._put_outbox(OutboxMessage(
                kind="agent",
                text=user_text,
                meta={"chain_id": chain_id, "peer_failure": True},
            ))
            self._events.emit(
                "peer_reply_failed_surfaced",
                chain_id=chain_id,
                peer=peer,
                reason=reason,
            )
            return

        try:
            # B55 R-7: pass the structured `[task_completed] kind=agent`
            # injection (= history's last entry, also user-role) so the
            # router sees the same lifecycle marker the SP rule covers
            # — parity with skill / plan completion paths.
            await self._run_router_loop(injected_text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_response): {exc}",
                meta={"chain_id": chain_id},
            ))
            return

    async def _resolve_pending_chain(
        self,
        pending: "_PendingChain",
        *,
        from_agent: str,
        last_response: str = "",
    ) -> None:
        """Drive a multi-hop pending chain forward by one delegate response.

        Drops ``from_agent`` from ``pending.waiting_on``. If others remain,
        no-op (the chain is still gathering replies). Otherwise, re-runs
        the router on the original request — by now every delegate's
        response is appended to history, so the LLM has all the material
        to compose a synthesized answer (or decide on more delegations,
        which keeps the chain pending).
        """
        from reyn.chat.session import RouterCapExceeded

        chain_id = pending.chain_id
        pending.waiting_on.discard(from_agent)
        if pending.waiting_on:
            # Partial resolution — record the new waiting_on for recovery.
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            return  # still waiting on other delegates

        # B2-H2 fix: if the peer's reply (the last incoming response) is a
        # no-reply marker, bypass the LLM and surface the failure deterministically.
        # Without this, weak models like gemini-2.5-flash-lite interpret the marker
        # as a normal conversational reply and emit a polite close (e.g.
        # "かしこまりました") while the user never learns the peer failed.
        if _is_no_reply_marker(last_response):
            parsed = _parse_no_reply_marker(last_response)
            peer = parsed[0] if parsed else from_agent
            reason = parsed[1] if parsed else "no reply produced"
            msg_template = _PEER_REPLY_FAILED_MSG.get(
                self._output_language or "en", _PEER_REPLY_FAILED_MSG["en"],
            )
            user_text = msg_template.format(peer=peer, reason=reason)
            # Forward the failure upstream — the pending chain always has an
            # origin_agent (chains from user requests don't reach _resolve_pending_chain;
            # they are handled by the `chain_id ∉ self._chains` branch above).
            await self.send_agent_response(
                to=pending.origin_agent,
                response=user_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
            )
            self._events.emit(
                "peer_reply_failed_surfaced",
                chain_id=chain_id,
                peer=peer,
                reason=reason,
                from_user=False,
            )
            await self._chains.resolve(chain_id)
            return

        # Arm delegation and reply capture for the re-run.
        self._set_router_loop_delegations([])
        self._set_router_loop_agent_replies([])
        try:
            await self._run_router_loop(pending.original_request, chain_id)
        except RouterCapExceeded as exc:
            # User-facing wrap-up (LLM summary or canned fallback) via shared fn.
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            # F6/F7 fix: structured marker upstream, not "". Always send regardless
            # of whether LLM wrap-up succeeded — user-facing and agent-protocol are
            # orthogonal: origin agent must know the chain ended on cap (#1538).
            await self.send_agent_response(
                to=pending.origin_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router retry budget exhausted ({exc.count}/{exc.cap}) "
                    f"resolving chain",
                ),
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (chain resolve): {exc}",
                meta={"chain_id": chain_id},
            ))
            # F6/F7 fix: structured marker upstream, not "".
            await self.send_agent_response(
                to=pending.origin_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router error during chain resolve: {exc}",
                ),
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        finally:
            new_dispatched = list(self._get_router_loop_delegations() or [])
            agent_replies = list(self._get_router_loop_agent_replies() or [])
            self._set_router_loop_delegations(None)
            self._set_router_loop_agent_replies(None)

        if new_dispatched:
            # Continue the chain with a fresh wave of delegations.
            pending.waiting_on = {d["to"] for d in new_dispatched}
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            # PR18: re-arm watchdog for the continued chain.
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        # F6/F7 fix: when no clean text reply was captured during chain
        # resolve, send a structured marker upstream rather than "".
        if agent_replies:
            final_reply = agent_replies[0]
        else:
            final_reply = _no_reply_marker(
                self.agent_name,
                "chain resolved without producing a text reply",
            )
        # History already appended by put_outbox.
        await self.send_agent_response(
            to=pending.origin_agent, response=final_reply,
            depth=pending.origin_depth, chain_id=chain_id,
        )
        await self._chains.resolve(chain_id)

    async def _emit_router_cap_exhausted_user(
        self, exc: Any, *, chain_id: str,
    ) -> None:
        """User-facing wrap-up when the per-turn router cap is reached.

        Delegates to ChatSession._emit_router_cap_exhausted_user (injected at
        construction) so both the SkillPlanGlue and A2AHandler paths call a
        single implementation — zero drift by construction (#1538).
        """
        await self._emit_router_cap_exhausted_fn(exc, chain_id=chain_id)


__all__ = ["A2AHandler"]
