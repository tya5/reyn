"""Tier 2: /model override propagates to RouterLoopDriver → RouterLoop.router_model.

#1746 integration gap: /model correctly set session._model_override, but
RouterLoopDriver captured model at construction time and RouterLoop was
constructed without router_model=, so the override never reached the LLM call.

Two-direction falsification:
- Override set   → RouterLoop receives the override class.
- Override unset → RouterLoop receives None (→ router-purpose-class default).

Uses RouterLoopDriver._loop_factory seam to capture router_model without
running a real LLM call. No MagicMock.
"""
from __future__ import annotations

import asyncio
import pytest

from reyn.chat.services.router_loop_driver import RouterLoopDriver
from reyn.llm.model_resolver import ModelResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _BailOut(Exception):
    """Raised by the capturing factory to exit run_turn immediately."""


class _FakeRouterHost:
    """Minimal host stub; RouterLoopDriver only reads .resolver from it."""

    def __init__(self, resolver: ModelResolver) -> None:
        self.resolver = resolver

    def _set_cancel_event(self, event):
        pass


class _FakeBudget:
    def check_and_increment_router_cap(self, text):
        pass

    def extend_router_cap(self, n):
        pass

    def add_router_usage(self, **kwargs):
        pass


class _FakeBudgetAdvisor:
    async def maybe_force_compact(self, **kwargs):
        pass


class _FakeEvents:
    def emit(self, *args, **kwargs):
        pass


def _make_resolver(*, default_class: str = "standard") -> ModelResolver:
    return ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o", "strong": "openai/gpt-4"},
        builtin={},
        default_class=default_class,
    )


def _make_driver(
    *,
    model_override_fn,
    resolver: ModelResolver,
    captured: list,
) -> RouterLoopDriver:
    """Build a minimal RouterLoopDriver with a _loop_factory that captures
    router_model and raises _BailOut so run_turn exits without a real LLM call."""

    def _capturing_factory(**kwargs):
        captured.append(kwargs.get("router_model"))
        raise _BailOut("captured")

    host = _FakeRouterHost(resolver)
    budget = _FakeBudget()

    return RouterLoopDriver(
        router_host=host,
        safety=None,
        router_max_iterations=1,
        budget_tracker=budget,
        non_interactive=True,
        exclude_tools=set(),
        budget=budget,
        resolver=resolver,
        compaction=None,
        compaction_controller=None,
        token_learner=None,
        events=_FakeEvents(),
        model_override_fn=model_override_fn,
        history_buffer=_FakeHistoryBuffer(),
        budget_advisor=_FakeBudgetAdvisor(),
        limit_checkpoint_fn=_noop_limit_checkpoint,
        next_seq_fn=lambda: 0,
        append_history_fn=lambda msg: None,
        _loop_factory=_capturing_factory,
    )


class _FakeHistoryBuffer:
    def build_history(self):
        return []

    def build_system_prompt(self):
        return ""


async def _noop_limit_checkpoint(**kwargs):
    from types import SimpleNamespace
    return SimpleNamespace(allow_continue=True, extension=1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_override_set_reaches_router_loop():
    """Tier 2: when _model_override="light", RouterLoop receives router_model="light".

    Falsification: without the fix (model_override_fn not read at run_turn),
    RouterLoop would receive router_model=None (the construction-time default)
    regardless of the override — this assertion would fail.
    """
    resolver = _make_resolver(default_class="standard")
    captured: list = []

    driver = _make_driver(
        model_override_fn=lambda: "light",
        resolver=resolver,
        captured=captured,
    )

    with pytest.raises(_BailOut):
        await driver.run_turn("hello", "chain1")

    assert captured == ["light"], (
        f"expected router_model='light' (override), got {captured!r}"
    )


@pytest.mark.asyncio
async def test_model_override_unset_passes_none_to_router_loop():
    """Tier 2: when no override is set, RouterLoop receives router_model=None.

    None → RouterLoop.resolve_purpose_class(None, resolver, "router") = config
    default — byte-identical to pre-/model behaviour, no regression.

    Falsification: if model_override_fn() were replaced with a lambda that
    always returned session.model (= "standard"), this test would fail because
    captured[0] would be "standard", not None.
    """
    resolver = _make_resolver(default_class="standard")
    captured: list = []

    driver = _make_driver(
        model_override_fn=lambda: None,
        resolver=resolver,
        captured=captured,
    )

    with pytest.raises(_BailOut):
        await driver.run_turn("hello", "chain1")

    assert captured == [None], (
        f"expected router_model=None (no override → RouterLoop resolves default), got {captured!r}"
    )


@pytest.mark.asyncio
async def test_model_override_changes_between_turns():
    """Tier 2: router_model is read live per turn — changing the override mid-session
    takes effect on the next turn.

    Falsification: if model_override_fn were evaluated once at construction and
    cached, the second turn would still use "light" instead of "strong".
    """
    resolver = _make_resolver(default_class="standard")
    captured: list = []

    current_override: list[str | None] = ["light"]

    driver = _make_driver(
        model_override_fn=lambda: current_override[0],
        resolver=resolver,
        captured=captured,
    )

    with pytest.raises(_BailOut):
        await driver.run_turn("first", "chain1")

    # Simulate user running /model strong
    current_override[0] = "strong"

    with pytest.raises(_BailOut):
        await driver.run_turn("second", "chain2")

    assert captured == ["light", "strong"], (
        f"expected live reads ['light', 'strong'], got {captured!r}"
    )
