"""FP-0005 — safety limits as checkpoints.

This package owns the cross-cutting "limit hit, what now?" decision
helper. Per-site raise paths (= ``LoopLimitExceededError``,
``RouterCapExceeded``, ``chain_timeout``, ``max_hop_depth`` refusal,
``PhaseBudgetExceededError``, etc.) call ``handle_limit_exceeded``
*before* aborting; the helper consults ``safety.on_limit.mode`` and
either pauses for user approval (interactive), auto-extends N times
(auto_extend), or falls through immediately (unattended, legacy).

The decision is intentionally separate from the raise: the helper
returns a ``LimitDecision`` and the caller decides how to extend the
counter / re-arm the deadline / continue. Keeping the helper raise-free
lets it stay site-agnostic (= one helper, six call sites) while each
site keeps its bespoke "extend the counter" logic.
"""
from reyn.limits.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)

__all__ = [
    "LimitDecision",
    "handle_limit_exceeded",
    "reset_run_extensions",
]
