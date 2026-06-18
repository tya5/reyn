"""Tier 2: /model override propagates to RouterLoopDriver → RouterLoop.router_model.

#1746 integration gap: /model correctly set session._model_override, but
RouterLoopDriver captured model at construction time and RouterLoop was
constructed without router_model=, so the override never reached the LLM call.

Two-direction falsification:
- Override set   → RouterLoop.router_model equals the override class.
- Override unset → RouterLoop.router_model equals the config default class.

Uses RouterLoopDriver._loop_observer seam: observer receives the constructed
RouterLoop and captures loop.router_model before run_turn proceeds. Literal
RouterLoop(...) construction is preserved so the #187 AST gate stays satisfied.
No MagicMock.
"""
from __future__ import annotations

import pytest

from reyn.chat.services.router_loop_driver import RouterLoopDriver
from reyn.llm.model_resolver import ModelResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _BailOut(Exception):
    """Raised by the observer to exit run_turn immediately after construction."""


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
    """Build a minimal RouterLoopDriver with a _loop_observer that captures
    loop.router_model and raises _BailOut so run_turn exits before LLM call."""

    def _capturing_observer(loop) -> None:
        captured.append(loop.router_model)
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
        _loop_observer=_capturing_observer,
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
    """Tier 2: when _model_override="light", RouterLoop.router_model == "light".

    Falsification: without the fix (model_override_fn not read at run_turn),
    RouterLoop would be constructed without the override and router_model would
    resolve to the config default ("standard") — the assertion would fail.
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
async def test_model_override_unset_resolves_config_default():
    """Tier 2: when no override is set, RouterLoop.router_model == config default.

    model_override_fn returns None → RouterLoop receives router_model=None →
    resolve_purpose_class(None, resolver, "router") → resolver.class_for_purpose("router")
    → default_class = "standard". Byte-identical to pre-/model behaviour, no regression.

    Falsification: if RouterLoopDriver hardcoded router_model="light" for all
    turns, loop.router_model would be "light" not "standard", and this assertion
    would fail.
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

    assert captured == ["standard"], (
        f"expected router_model='standard' (config default, no override), got {captured!r}"
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
