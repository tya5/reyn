"""SkillPlanGlue — skill/plan completion routing and chain timeout for Session.

Extracted from Session (session.py refactor PR-4 — FP-0019 series final).
Owns the cluster that routes completed skill/plan work back into the router
loop and handles chain timeout lifecycle:

  - handle_skill_completed(payload)        — was _handle_skill_completed
  - drain_skill_completed_inbox(deadline)  — was drain_skill_completed_inbox
  - on_chain_timeout_fire(chain_id)        — was _on_chain_timeout_fire
  - spawn_skill_for_router(spec, chain_id) — was _spawn_skill_for_router
  - spawn_skill(spec, chain_id)            — was _spawn_skill
  - enqueue_plan_completed(**kw)           — was _enqueue_plan_completed

Public-surface note: ``drain_skill_completed_inbox`` is called from
``mcp_server.py`` (R-A2A-COMPLETION-DRAIN); Session keeps a forwarding
wrapper so the external call site is unaffected.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SkillPlanGlue:
    """Routes skill/plan completions into router narration turns; handles chain
    timeout lifecycle.

    Constructed once per Session.  All state (inbox, chains, journal) is
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
        inbox: asyncio.Queue,                  # Session.inbox
        journal: Any,                          # SnapshotJournal
        on_limit: Any,                         # SafetyOnLimitConfig
        chains: Any,                           # ChainManager
        limit_checkpoint_fn: Callable,         # async (**kw) → LimitDecision
        chain_timeout_seconds: float,
        send_agent_response_fn: Callable,      # async (to, response, depth, chain_id)
        skill_runner: Any,                     # SkillRunner
        put_inbox_fn: Callable,               # async (kind, payload) → None
    ) -> None:
        self._append_history = append_history_fn
        self._events = events
        self._reset_turn_counter = reset_turn_counter_fn
        self._run_router_loop = run_router_loop_fn
        self._emit_cap_exhausted = emit_cap_exhausted_fn
        self._put_outbox = put_outbox_fn
        self._inbox = inbox
        self._journal = journal
        self._on_limit = on_limit
        self._chains = chains
        self._limit_checkpoint = limit_checkpoint_fn
        self._chain_timeout_seconds = chain_timeout_seconds
        self._send_agent_response = send_agent_response_fn
        self._skill_runner = skill_runner
        self._put_inbox = put_inbox_fn

    # ── Skill completion narration (FP-0012) ──────────────────────────────────

    async def handle_skill_completed(self, payload: dict) -> None:
        """FP-0012: drive narration of a background-spawned skill's completion.

        Injects a synthesized ``user``-role message carrying the structured
        completion data, then runs one router LLM turn so the LLM narrates.
        ``meta.source="skill_completion"`` distinguishes this from genuine
        user input in audit / replay paths.
        """
        import json as _json

        from reyn.chat.session import ChatMessage, RouterCapExceeded, _new_chain_id, _now_iso

        run_id = payload.get("run_id", "")
        skill_name = payload.get("skill", "")
        status = payload.get("status") or "finished"
        chain_id_raw = payload.get("chain_id") or ""
        chain_id = chain_id_raw or _new_chain_id()
        data = payload.get("data") or {}

        try:
            data_str = _json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            data_str = repr(data)
        injected_text = (
            f"[task_completed] kind=skill run_id={run_id} chain_id={chain_id}\n"
            f"skill: {skill_name}  status: {status}\n"
            f"result: {data_str}"
        )

        self._append_history(ChatMessage(
            role="user", content=injected_text, ts=_now_iso(),
            meta={
                "source": "skill_completion",
                "skill": skill_name,
                "run_id": run_id,
                "status": status,
                "chain_id": chain_id,
            },
        ))
        self._events.emit(
            "skill_completion_injected",
            run_id=run_id, skill=skill_name, status=status, chain_id=chain_id,
        )

        # Reset the per-turn router cap — completion narration is a fresh turn.
        self._reset_turn_counter()

        try:
            await self._run_router_loop(injected_text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_cap_exhausted(exc, chain_id=chain_id)
            return
        except Exception as exc:
            from reyn.chat.session import OutboxMessage
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (skill_completed): {exc}",
                meta={"chain_id": chain_id, "skill": skill_name, "run_id": run_id},
            ))

    # ── A2A bypass drain (R-A2A-COMPLETION-DRAIN) ────────────────────────────

    async def drain_skill_completed_inbox(
        self, *, deadline_monotonic: float,
    ) -> bool:
        """FP-0012 / R-A2A-COMPLETION-DRAIN: dispatch queued
        ``skill_completed`` inbox kinds inline up to a deadline.

        Returns ``True`` if the drain completed before the deadline,
        ``False`` if the deadline fired mid-drain (= partial reply).
        """
        import time as _time

        deferred: list[tuple[str, dict]] = []
        drained_ok = True
        while True:
            try:
                item = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            kind, payload = item
            if kind != "skill_completed":
                deferred.append(item)
                continue
            msg_id = (
                payload.get("_msg_id") if isinstance(payload, dict) else None
            )
            try:
                await self._journal.consume_inbox(msg_id=msg_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "drain_skill_completed_inbox: WAL consume failed "
                    "msg_id=%s: %s",
                    msg_id, exc,
                )
            remaining = max(0.1, deadline_monotonic - _time.monotonic())
            try:
                async with asyncio.timeout(remaining):
                    await self.handle_skill_completed(payload)
            except asyncio.TimeoutError:
                drained_ok = False
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "drain_skill_completed_inbox: handler failed "
                    "run_id=%s skill=%s: %s",
                    payload.get("run_id"), payload.get("skill"), exc,
                )
        for item in deferred:
            self._inbox.put_nowait(item)
        return drained_ok

    # ── Chain timeout (PR18 / FP-0005) ───────────────────────────────────────

    async def on_chain_timeout_fire(self, chain_id: str) -> None:
        """ChainManager invokes this when a chain's timeout watchdog fires.

        Pops the pending chain, emits the ``chain_timeout`` audit event, and
        synthesises an error response upstream so the parent chain doesn't hang.

        FP-0005: when ``safety.on_limit.mode`` opts in (interactive /
        auto_extend), asks whether to re-arm with a fresh deadline before firing.
        ``unattended`` (= default) preserves the legacy fire-and-error behaviour.
        """
        from reyn.chat.session import OutboxMessage

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
            )
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain timeout: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    # ── Skill spawning delegation ─────────────────────────────────────────────

    async def spawn_skill_for_router(
        self, spec: dict, *, chain_id: str
    ) -> dict:
        """Thin delegation to SkillRunner.spawn_for_router (FP-0019 Wave 1b)."""
        return await self._skill_runner.spawn_for_router(spec, chain_id=chain_id)

    async def spawn_skill(self, spec: dict, *, chain_id: str | None = None) -> None:
        """Thin delegation to SkillRunner.spawn (FP-0019 Wave 1b)."""
        await self._skill_runner.spawn(spec, chain_id=chain_id)

    # ── Plan completion enqueue (FP-0025 C) ───────────────────────────────────

    async def enqueue_plan_completed(
        self,
        *,
        plan_id: str,
        chain_id: str,
        goal: str,
        step_results: dict[str, str],
        step_failures: dict[str, str],
        n_steps: int,
    ) -> None:
        """FP-0025 C: enqueue plan_completed inbox for router narration."""
        try:
            await self._put_inbox(
                "plan_completed",
                {
                    "plan_id": plan_id,
                    "chain_id": chain_id,
                    "goal": goal,
                    "step_results": step_results,
                    "step_failures": step_failures,
                    "n_steps": n_steps,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_enqueue_plan_completed failed for %s: %r", plan_id, exc,
            )
