"""Tier 2: OS invariant — eval_benchmark dispatch uses no interactive bus.

PR-N9 (FP-0008): sandbox_2 13977 dogfood hung 4 hours at apply visit 6
boundary because ``eval_benchmark.py:350`` wired an interactive
``StdinInterventionBus`` into the Agent. When the safety limit
checkpoint fired (apply phase max_phase_visits cap exceeded), the bus
tried to read from tty raw_mode via ``prompt_toolkit.PromptSession``,
blocking the asyncio event loop indefinitely in a non-interactive
subprocess.

Fix: pass ``intervention_bus=None`` so the existing
``safety/limit_handler.py:173-179`` ``no_bus`` clean-abort path fires
instead. This file pins both halves of that contract:

1. **Wiring invariant**: the eval_benchmark module no longer imports or
   instantiates ``StdinInterventionBus``. Caught at import time and via
   source-text inspection so a future re-introduction surfaces in CI.
2. **Receiver invariant**: with ``bus=None`` and the default interactive
   ``OnLimitConfig``, ``handle_limit_exceeded`` returns a clean
   ``no_bus`` ``LimitDecision`` with ``allow_continue=False`` — the same
   path other headless callers (= dispatch_tool / scripted runs) rely on.

No mocks. Real ``handle_limit_exceeded`` invocation; real source-text
inspection of the eval_benchmark module.
"""
from __future__ import annotations

import asyncio
import inspect

from reyn.cli.commands import eval_benchmark
from reyn.config import OnLimitConfig
from reyn.limits.limit_handler import handle_limit_exceeded


def test_eval_benchmark_does_not_import_stdininterventionbus() -> None:
    """Tier 2: eval_benchmark module source must not import StdinInterventionBus.

    A re-introduced import is a strong signal that the wiring regressed
    to the pre-PR-N9 interactive-bus path. We use source-text inspection
    (``inspect.getsource``) rather than ``importlib.util`` AST walks
    because the regression we care about is the literal source line —
    that's what reviewers and merge conflicts touch.
    """
    src = inspect.getsource(eval_benchmark)
    assert "StdinInterventionBus" not in src, (
        "eval_benchmark.py references StdinInterventionBus — "
        "PR-N9 wired it out to avoid prompt_async hang in non-interactive "
        "subprocess context. Re-introducing the import is a regression."
    )


def test_eval_benchmark_agent_call_passes_intervention_bus_none() -> None:
    """Tier 2: the Agent(...) construction site in eval_benchmark passes
    ``intervention_bus=None`` so the safety_helper no_bus path fires at
    limit-checkpoint boundaries.

    Source-text regression guard: pins the literal kwarg so a future edit
    that drops the ``None`` (or re-adds an interactive bus) surfaces in
    CI rather than at the next 4-hour dogfood hang.
    """
    src = inspect.getsource(eval_benchmark)
    # The kwarg must appear literally; ordering / surrounding whitespace
    # is incidental. We assert presence, not column alignment.
    assert "intervention_bus=None" in src, (
        "eval_benchmark.py does not pass intervention_bus=None to the "
        "benchmark-time Agent constructor. PR-N9 requires this for the "
        "no_bus clean-abort path to fire instead of hanging on tty."
    )


def test_handle_limit_exceeded_with_bus_none_returns_no_bus_decision() -> None:
    """Tier 2: receiver-side contract — handle_limit_exceeded(bus=None)
    with default interactive OnLimitConfig returns a clean ``no_bus``
    decision, never blocking on a bus that doesn't exist.

    This is the behavior the PR-N9 wiring fix relies on. If this
    invariant breaks, a benchmark with ``intervention_bus=None`` would
    no longer abort cleanly at limit checkpoints.
    """
    on_limit = OnLimitConfig()  # default mode = "interactive"
    assert on_limit.mode == "interactive", (
        "Default OnLimitConfig mode changed; this test assumed "
        "'interactive' as the default and the PR-N9 fix relied on it."
    )

    decision = asyncio.run(
        handle_limit_exceeded(
            bus=None,
            on_limit=on_limit,
            kind="max_phase_visits",
            run_id="benchmark-run",
            prompt="Phase visit cap exceeded — extend?",
        )
    )

    assert decision.allow_continue is False, (
        "no_bus path must NOT allow continuation; benchmark would hang "
        "trying to extend with no bus to ask"
    )
    assert decision.reason == "no_bus", (
        f"no_bus path must surface reason='no_bus' for audit; got {decision.reason!r}"
    )
    assert decision.extension == 0.0


def test_handle_limit_exceeded_with_bus_none_under_auto_extend_falls_through_to_unattended() -> None:
    """Tier 2: when auto_extend budget is exhausted and bus is None, the
    fall-through abort surfaces as ``unattended`` (= existing behavior;
    PR-N9 wiring does not regress this path).
    """
    on_limit = OnLimitConfig(mode="auto_extend", auto_extend_times=0)

    decision = asyncio.run(
        handle_limit_exceeded(
            bus=None,
            on_limit=on_limit,
            kind="router_cap",
            run_id="benchmark-run",
            prompt="Router cap exceeded — extend?",
        )
    )

    assert decision.allow_continue is False
    assert decision.reason == "unattended"
    assert decision.extension == 0.0
