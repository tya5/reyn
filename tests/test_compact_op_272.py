"""Tier 2: OS invariant — #272/#1128 compact op (voluntary history compaction).

Covers the op handler contract + the OS-injected context-size signal renderer
with REAL collaborators (real execute_op, real OpContext, a hand-written async
capability — no mocks):
  - compact routes to the caller-wired OpContext.compact_now and passes through
    its exact-token result ({freed_tokens, free_window_after});
  - fail-loud when unwired (compaction_unavailable) — never a silent no-op;
  - a raising capability surfaces as compaction_failed, never crashes the turn;
  - the context-size signal is gated: None when the window is ample, a header
    when filling, and a compact nudge when low (exact-token, unit-aligned).

The chat/phase wiring of ``compact_now`` itself is exercised end-to-end in the
axis wiring tests; here the op + signal contracts are pinned in isolation.
"""
from __future__ import annotations

import asyncio

from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl
from reyn.schemas.models import CompactIROp
from reyn.services.compaction.context_signal import render_context_size_signal


class _Events:
    def __init__(self) -> None:
        self.subscribers: list = []
        self.emitted: list[tuple] = []

    def emit(self, *args, **kw):  # noqa: ANN002, ANN003
        self.emitted.append((args[0] if args else None, kw))

    def types(self) -> list:
        return [t for t, _ in self.emitted]


def _ctx(compact_now=None) -> OpContext:
    return OpContext(
        workspace=None,
        events=_Events(),
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        skill_name="",
        compact_now=compact_now,
    )


# ── op handler contract ──────────────────────────────────────────────────────


def test_compact_routes_to_capability_and_passes_exact_tokens() -> None:
    """Tier 2: compact calls ctx.compact_now and returns its exact-token result."""
    async def _cap() -> dict:
        return {"freed_tokens": 1200, "free_window_after": 8000, "free_window_before": 6800}

    ctx = _ctx(_cap)
    op = CompactIROp(kind="compact", reason="window low before big read")
    result = asyncio.run(execute_op(op, ctx, caller="control_ir"))
    assert result["status"] == "ok"
    assert result["freed_tokens"] == 1200
    assert result["free_window_after"] == 8000
    assert "compact_op_completed" in ctx.events.types()


def test_compact_fail_loud_when_unwired() -> None:
    """Tier 2: no compaction context → structured compaction_unavailable error,
    never a silent no-op (the model must not believe the window was freed)."""
    ctx = _ctx(None)
    result = asyncio.run(execute_op(CompactIROp(kind="compact"), ctx, caller="control_ir"))
    assert result["status"] == "error"
    assert result["error_kind"] == "compaction_unavailable"
    assert "freed_tokens" not in result, "must not fabricate freed tokens when unavailable"


def test_compact_capability_raise_becomes_error_never_crashes() -> None:
    """Tier 2: a raising compaction surfaces as compaction_failed, not an exception."""
    async def _boom() -> dict:
        raise RuntimeError("force_compact_race_unrecovered")

    result = asyncio.run(execute_op(CompactIROp(kind="compact"), _ctx(_boom), caller="control_ir"))
    assert result["status"] == "error"
    assert result["error_kind"] == "compaction_failed"
    assert "force_compact_race_unrecovered" in result["error"]


# ── context-size signal (OS-injected, gated, exact-token) ──────────────────────


def test_signal_none_when_window_ample() -> None:
    """Tier 2: ample window → no signal (no per-turn SP noise / fixture churn)."""
    assert render_context_size_signal(free_window=90_000, effective_trigger=100_000) is None
    # degenerate / unknown trigger → no signal (can't reason about a 0 window).
    assert render_context_size_signal(free_window=0, effective_trigger=0) is None


def test_signal_appears_when_filling_with_exact_tokens() -> None:
    """Tier 2: filling window → header with exact-token used/free numbers."""
    sig = render_context_size_signal(free_window=40_000, effective_trigger=100_000)
    assert sig is not None
    assert "Context window" in sig
    assert "60000" in sig and "40000" in sig  # exact used/free tokens, not buckets


_NUDGE = "If you still have work to do"  # distinctive marker (header always says "auto-compaction")


def test_signal_nudges_compact_only_when_low() -> None:
    """Tier 2: a low free window adds the explicit compact nudge; a merely-filling
    one shows the budget without nudging."""
    low = render_context_size_signal(free_window=10_000, effective_trigger=100_000)
    assert low is not None and _NUDGE in low
    filling = render_context_size_signal(free_window=40_000, effective_trigger=100_000)
    assert filling is not None and _NUDGE not in filling
