"""SkillPlanGlue — chain timeout lifecycle for Session.

Handles the chain timeout lifecycle:

  - on_chain_timeout_fire(chain_id)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SkillPlanGlue:
    """Handles chain timeout lifecycle for Session.

    Constructed once per Session.  All state (chains, journal) is
    provided at construction time; this class owns no durable state of its own.
    """

    def __init__(
        self,
        *,
        append_history_fn: Callable,           # Session._append_history
        events: Any,                           # EventLog — emit events
        reset_turn_counter_fn: Callable,       # Session._reset_router_turn_counter
        run_router_loop_fn: Callable,          # async (text, chain_id) → None
        emit_cap_exhausted_fn: Callable,       # async (exc, *, chain_id) → None
        put_outbox_fn: Callable,               # async OutboxMessage → None
        inbox: Any,                            # Session.inbox (retained for ctor compat)
        journal: Any,                          # SnapshotJournal
        on_limit: Any,                         # SafetyOnLimitConfig
        chains: Any,                           # ChainManager
        limit_checkpoint_fn: Callable,         # async (**kw) → LimitDecision
        chain_timeout_seconds: float,
        send_agent_response_fn: Callable,      # async (to, response, depth, chain_id)
        put_inbox_fn: Callable,               # async (kind, payload) → None
    ) -> None:
        self._append_history = append_history_fn
        self._events = events
        self._reset_turn_counter = reset_turn_counter_fn
        self._run_router_loop = run_router_loop_fn
        self._emit_cap_exhausted = emit_cap_exhausted_fn
        self._put_outbox = put_outbox_fn
        self._on_limit = on_limit
        self._chains = chains
        self._limit_checkpoint = limit_checkpoint_fn
        self._chain_timeout_seconds = chain_timeout_seconds
        self._send_agent_response = send_agent_response_fn

    # ── Chain timeout (PR18 / FP-0005) ───────────────────────────────────────

    async def on_chain_timeout_fire(self, chain_id: str) -> None:
        """ChainManager invokes this when a chain's timeout watchdog fires.

        Pops the pending chain, emits the ``chain_timeout`` audit event, and
        synthesises an error response upstream so the parent chain doesn't hang.

        FP-0005: when ``safety.on_limit.mode`` opts in (interactive /
        auto_extend), asks whether to re-arm with a fresh deadline before firing.
        ``unattended`` (= default) preserves the legacy fire-and-error behaviour.
        """
        from reyn.runtime.session import OutboxMessage

        if self._on_limit.mode != "unattended":
            pending_peek = self._chains.get(chain_id)
            if pending_peek is not None:
                waiting_peek = sorted(pending_peek.waiting_on)
                decision = await self._limit_checkpoint(
                    kind=f"chain_seconds:{chain_id}",
                    prompt=(
                        f"Chain {chain_id} timed out waiting for "
                        f"{', '.join(waiting_peek) or 'unknown'} after "
                        f"{self._chain_timeout_seconds:g}s. Wait longer?"
                    ),
                    detail=(
                        f"chain={chain_id} waiting_on={waiting_peek} "
                        f"timeout={self._chain_timeout_seconds:g}s"
                    ),
                    extension_amount=float(self._chain_timeout_seconds),
                    run_id=chain_id,
                )
                if decision.allow_continue:
                    self._chains.arm_timeout(
                        chain_id, on_fire=self.on_chain_timeout_fire,
                    )
                    self._events.emit(
                        "chain_timeout_extended",
                        chain_id=chain_id,
                        waiting_on=waiting_peek,
                        extension_seconds=decision.extension,
                        reason=decision.reason,
                    )
                    return
        pending = await self._chains.fire_timeout(chain_id)
        if pending is None:
            return
        waiting = sorted(pending.waiting_on)
        error_text = (
            f"chain timeout: {len(waiting)} delegate(s) "
            f"({', '.join(waiting) or 'unknown'}) did not respond within "
            f"{self._chain_timeout_seconds:g}s. "
            f"→ Raise safety.timeout.chain_seconds to wait longer "
            f"(0 = no timeout)."
        )
        self._events.emit(
            "chain_timeout",
            chain_id=chain_id,
            waiting_on=waiting,
            timeout_seconds=self._chain_timeout_seconds,
            origin_agent=pending.origin_agent,
        )
        try:
            await self._send_agent_response(
                to=pending.origin_agent,
                response=error_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
                to_sid=getattr(pending, "origin_sid", None),  # #2130
            )
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain timeout: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

