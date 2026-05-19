"""FP-0005 — generic safety-limit checkpoint helper.

Six sites in the codebase raise on a safety limit hit:

  - B (max_phase_visits)      — ``OSRuntime._enter_phase``
  - F (phase_seconds)         — ``OSRuntime._check_phase_budget``
  - A (max_act_turns)         — ``skill_node_runner`` act-loop
  - C (router_cap)            — ``BudgetGateway.check_and_increment_router_cap``
  - E (max_hop_depth)         — ``ChatSession._send_to_agent``
  - G (chain_seconds)         — ``ChainManager`` watchdog fire path

Plus FP-0003 already covers:

  - D (per_chain_skill_calls) — ``ChatSession._ask_budget_extension``

This module replaces the bespoke ``_ask_budget_extension`` with a
generic ``handle_limit_exceeded`` callable that all seven sites share.
The signature is intentionally minimal: the caller passes the user-
facing ``prompt``, machine-readable ``kind``, and per-site
``extension_amount`` (= "if approved, by how much?"); the helper
consults the ``OnLimitConfig`` mode and returns a ``LimitDecision``.

The helper itself never raises; the caller decides whether to abort
when the decision says ``allow_continue=False``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from reyn.config import OnLimitConfig
from reyn.user_intervention import (
    InterventionChoice,
    RequestBus,
    UserIntervention,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LimitDecision:
    """Outcome of a safety-limit checkpoint (FP-0005).

    Fields:
        allow_continue:
            ``True`` → caller should extend the relevant counter and
            continue. ``False`` → caller should fall through to its
            legacy abort path.
        extension:
            Site-specific magnitude of the extension. For count-based
            limits (max_visits, router_cap, max_hop_depth) this is an
            integer count; for time-based limits (phase_seconds,
            chain_seconds) it is seconds. The helper returns the value
            the caller passed in as ``extension_amount`` when approved,
            and ``0`` on refusal.
        reason:
            Stable string describing the decision path. One of
            ``"user_approved"`` / ``"auto_extended"`` / ``"user_refused"`` /
            ``"ask_timeout"`` / ``"unattended"`` / ``"no_bus"``.
            Surfaced into events for audit and into error messages so
            operators can see why a run was aborted.
    """

    allow_continue: bool
    extension: float
    reason: str


# Process-local bookkeeping for ``auto_extend`` mode. Keyed by
# ``(run_id, kind)`` so each (run, limit) combo has its own counter.
# Not persisted: ``auto_extend_times`` is "this run only".
_auto_extend_used: dict[tuple[str, str], int] = {}


def reset_run_extensions(run_id: str) -> None:
    """Reset auto_extend bookkeeping for ``run_id``.

    Call at the start of a run (= ``OSRuntime.run`` entry,
    ``ChatSession`` turn boundary). After this, ``auto_extend_times``
    grants are fresh.
    """
    keys_to_drop = [k for k in _auto_extend_used if k[0] == run_id]
    for k in keys_to_drop:
        del _auto_extend_used[k]


def _yes_no_choices() -> list[InterventionChoice]:
    """The standard yes/no prompt used by all safety-limit checkpoints.

    Mirrors FP-0003's ``_ask_budget_extension`` choice set so the chat
    UI behaves consistently across permission gates and limit gates.
    """
    return [
        InterventionChoice(id="yes", label="[Y]es, continue", hotkey="y"),
        InterventionChoice(id="no", label="[N]o, abort", hotkey="n"),
    ]


async def handle_limit_exceeded(
    *,
    bus: Optional[RequestBus],
    on_limit: OnLimitConfig,
    kind: str,
    run_id: str,
    prompt: str,
    detail: str = "",
    extension_amount: float = 1.0,
    skill_name: str | None = None,
) -> LimitDecision:
    """Generic safety-limit checkpoint dispatcher (FP-0005).

    Mode dispatch:
      - ``unattended`` (default): return ``allow_continue=False``
        immediately. Caller falls through to legacy abort.
      - ``auto_extend``: increment per-(run_id, kind) counter; allow
        if within ``auto_extend_times``, else fall through.
      - ``interactive``: dispatch a yes/no ``UserIntervention`` via
        ``bus.request`` with a timeout of
        ``on_limit.ask_timeout_seconds``. Allow on yes, refuse on no
        / unrecognised choice / timeout.

    Args:
        bus: The intervention bus to dispatch on. ``None`` is treated
             as ``unattended`` regardless of mode (= "no bus, no UX
             surface, fail closed").
        on_limit: The ``safety.on_limit`` config.
        kind: Stable machine-readable limit identifier
              (e.g. ``"max_phase_visits"``, ``"router_cap"``). Used
              for event audit + as part of the ``UserIntervention.kind``
              namespace (``safety.limit.<kind>``).
        run_id: Stable run identifier used for the auto_extend counter.
                Pass the OSRuntime run_id, plan_id, or chain_id —
                whichever scopes the extension correctly for this site.
        prompt: User-facing question text.
        detail: Optional second line of context (= specifics like
                "Phase 'revise' visit count 25 / 25").
        extension_amount: How much to extend the counter by on approval.
        skill_name: Optional skill name for /list / TUI display.

    Returns:
        LimitDecision describing the outcome. Caller is responsible for
        applying the extension when ``allow_continue=True`` and for
        aborting otherwise.
    """
    if on_limit.mode == "unattended":
        return LimitDecision(
            allow_continue=False, extension=0.0, reason="unattended",
        )

    if on_limit.mode == "auto_extend":
        key = (run_id, kind)
        used = _auto_extend_used.get(key, 0)
        if used < on_limit.auto_extend_times:
            _auto_extend_used[key] = used + 1
            _logger.info(
                "safety.limit auto-extended (kind=%s run=%s used=%d/%d)",
                kind, run_id, used + 1, on_limit.auto_extend_times,
            )
            return LimitDecision(
                allow_continue=True,
                extension=extension_amount,
                reason="auto_extended",
            )
        # Auto-extend budget exhausted — fall through to abort.
        return LimitDecision(
            allow_continue=False, extension=0.0, reason="unattended",
        )

    # interactive
    if bus is None:
        # No bus → no way to ask. Behave as unattended so headless
        # callers (= dispatch_tool / scripted runs) abort silently
        # instead of hanging on a bus that doesn't exist.
        return LimitDecision(
            allow_continue=False, extension=0.0, reason="no_bus",
        )

    iv = UserIntervention(
        kind=f"safety.limit.{kind}",
        prompt=prompt,
        detail=detail,
        choices=_yes_no_choices(),
        run_id=run_id,
        skill_name=skill_name,
    )
    try:
        if on_limit.ask_timeout_seconds > 0:
            answer = await asyncio.wait_for(
                bus.request(iv),
                timeout=on_limit.ask_timeout_seconds,
            )
        else:
            # 0 / negative → no timeout. Wait forever. Use this for
            # interactive sessions that genuinely need to wait for the
            # human (= reyn chat sitting at a TUI).
            answer = await bus.request(iv)
    except asyncio.TimeoutError:
        _logger.info(
            "safety.limit ask timed out (kind=%s run=%s after %.1fs)",
            kind, run_id, on_limit.ask_timeout_seconds,
        )
        return LimitDecision(
            allow_continue=False, extension=0.0, reason="ask_timeout",
        )
    except Exception:
        # Bus failure (cancellation / disconnect / unexpected error)
        # = treat as refusal. Failing open here would let limit hits
        # silently bypass via a flaky bus.
        _logger.warning(
            "safety.limit bus.request raised (kind=%s run=%s) — "
            "treating as refusal.", kind, run_id,
            exc_info=True,
        )
        return LimitDecision(
            allow_continue=False, extension=0.0, reason="user_refused",
        )

    choice = getattr(answer, "choice_id", None)
    if choice == "yes":
        return LimitDecision(
            allow_continue=True,
            extension=extension_amount,
            reason="user_approved",
        )
    return LimitDecision(
        allow_continue=False, extension=0.0, reason="user_refused",
    )
