"""Tier 2: #2210 — the LLM-call HTTP timeout (router-path wiring + on_limit integration).

Two layers, both falsified here (real `OnLimitConfig` + a real bus fake, no mocks — same
pattern as `test_budget_limit_unify_1868`):

LOW (`_resolve_llm_call_bounds`) — the per-call timeout precedence. An EXPLICIT timeout
(the kernel path threads `safety.timeout.llm_call_seconds`) WINS; only a `None` timeout (the
router path) falls back to the ambient policy context. This is the kernel-regression guard:
the kernel's explicit value must NOT be overridden by the ambient context.

HIGH (`_llm_timeout_allows_continue`) — a persistent provider hang (per-call timeout + retries
exhausted) routes through the SAME `handle_limit_exceeded` + `safety.on_limit` framework the
budget gate uses, instead of a bare error. interactive yes → retry; no → surface; unset →
fail-closed; auto_extend bounded → a hung provider cannot retry forever.
"""
from __future__ import annotations

import pytest

import reyn.llm.llm as llm_mod
from reyn.config.chat import OnLimitConfig
from reyn.llm.llm import (
    _llm_timeout_allows_continue,
    _resolve_llm_call_bounds,
    set_llm_call_limit_context,
)
from reyn.user_intervention import InterventionAnswer


@pytest.fixture(autouse=True)
def _reset_ctx():
    llm_mod._llm_call_limit_context_var.set(None)
    yield
    llm_mod._llm_call_limit_context_var.set(None)


class _Bus:
    """Minimal RequestBus fake (mirrors test_budget_limit_unify_1868._Bus)."""

    def __init__(self, choice: "str | None") -> None:
        self._choice = choice
        self.asked = False
        self.last_kind: "str | None" = None

    async def request(self, iv) -> InterventionAnswer:  # type: ignore[no-untyped-def]
        self.asked = True
        self.last_kind = getattr(iv, "kind", None)
        return InterventionAnswer(text="", choice_id=self._choice)


# ── LOW: per-call timeout precedence (kernel-regression guard) ─────────────────


def test_explicit_timeout_wins_over_ambient_kernel_unchanged():
    """Tier 2: an EXPLICIT timeout (the kernel path) is returned AS-IS even when the ambient
    context carries a different value — the kernel behaviour is unchanged. RED if the
    generalize made the ambient override the explicit param (a kernel regression)."""
    set_llm_call_limit_context(
        _Bus(None), OnLimitConfig(), "run", False,
        llm_call_timeout=999.0, llm_max_retries=9)
    timeout, retries = _resolve_llm_call_bounds(60.0, 1)  # kernel passes explicit 60.0 / 1
    assert timeout == 60.0, "explicit timeout must win over the ambient context"
    assert retries == 1, "explicit retries must win over the ambient context"


def test_none_timeout_falls_back_to_ambient_router_path():
    """Tier 2: a None timeout (the router path passes no timeout) falls back to the ambient
    context's value. RED if the fallback is unwired (timeout stays None → litellm default →
    a hung provider hangs the turn — the #2210 bug)."""
    set_llm_call_limit_context(
        _Bus(None), OnLimitConfig(), "run", False,
        llm_call_timeout=42.0, llm_max_retries=3)
    timeout, retries = _resolve_llm_call_bounds(None, 1)  # router passes None / default 1
    assert timeout == 42.0, "router path must inherit the ambient per-call timeout"
    assert retries == 3, "router path must inherit the ambient retry budget"


def test_none_timeout_no_context_stays_none():
    """Tier 2: None timeout + no policy context → stays None (litellm default, the pre-#2210
    behaviour — no worse, and direct/test construction is unaffected)."""
    timeout, retries = _resolve_llm_call_bounds(None, 1)
    assert timeout is None and retries == 1


# ── HIGH: persistent-timeout → on_limit (framework integration) ───────────────


@pytest.mark.asyncio
async def test_unset_context_fails_closed():
    """Tier 2: no policy context → the timeout gate fails CLOSED (no retry; surface the
    timeout). A hung provider with no runtime policy must not silently retry."""
    assert await _llm_timeout_allows_continue("m", "timed out") is False


@pytest.mark.asyncio
async def test_interactive_yes_retries():
    """Tier 2: interactive + user says YES → retry once more (a fresh timeout window). The
    timeout must reach the user as a timeout-kind intervention. RED if the HIGH layer does
    not route the persistent timeout through on_limit (a bare error instead)."""
    bus = _Bus("yes")
    set_llm_call_limit_context(bus, OnLimitConfig(mode="interactive"), "run-y", False)
    assert await _llm_timeout_allows_continue("gpt-x", "timed out") is True
    assert bus.asked and bus.last_kind == "safety.limit.timeout.llm_call", (
        "the persistent timeout must reach the user as a timeout-kind intervention"
    )


@pytest.mark.asyncio
async def test_interactive_no_surfaces():
    """Tier 2: (falsification) interactive + user says NO → no retry; surface the timeout
    (clean turn-end). If the gate ignored the answer this would wrongly retry."""
    bus = _Bus("no")
    set_llm_call_limit_context(bus, OnLimitConfig(mode="interactive"), "run-n", False)
    assert await _llm_timeout_allows_continue("gpt-x", "timed out") is False


@pytest.mark.asyncio
async def test_auto_extend_is_bounded():
    """Tier 2: auto_extend retries up to auto_extend_times then denies — a hung provider
    CANNOT retry forever (bounded by construction, reusing the existing _bounded_auto_extend).
    RED if the timeout retry is unbounded."""
    set_llm_call_limit_context(
        _Bus(None), OnLimitConfig(mode="auto_extend", auto_extend_times=1), "run-ax", False)
    first = await _llm_timeout_allows_continue("m", "timed out")
    second = await _llm_timeout_allows_continue("m", "timed out")
    assert first is True and second is False, "timeout auto_extend is bounded (1 retry, then surface)"
