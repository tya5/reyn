"""Tier 2: high-cost model BLOCK gate (#1867 / FP-0052 S4).

`maybe_block_high_cost_model` is the optional blocking confirm layered on top
of the #1830/#1861 warn-only feature. When `cost_warn.block_on_high_cost` is
on and the target is high-cost, a `/model` switch is held for an interactive
confirm routed through the unified safety framework
(`session._handle_chat_limit_checkpoint` → `handle_limit_exceeded`). The switch
applies only on approval; a decline — or a non-interactive session
(fail-closed) — blocks it.

Falsification anchors:
- not-bespoke: the confirm is dispatched with kind="cost.high_cost_model"
  through the session's limit-checkpoint wrapper (a custom block path would
  never call it).
- fail-closed: a non-interactive session returns False WITHOUT dispatching a
  confirm (a prompt would hang) — proving the pre-checkpoint guard.
- opt-in: block_on_high_cost=False never calls the checkpoint (warn-only).
- no-gate for cheap: a below-threshold model is never gated.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config.chat import CostWarnConfig
from reyn.llm.model_cost_rate import get_input_cost_per_1m_usd
from reyn.runtime.model_cost_warn import maybe_block_high_cost_model


class _FakeEventLog:
    def __init__(self) -> None:
        self._emitted: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: object) -> None:
        self._emitted.append((event_type, dict(data)))

    def snapshot(self) -> list[tuple[str, dict]]:
        return list(self._emitted)


class _FakeResolver:
    def resolve(self, name: str) -> object:
        class _Spec:
            model = name
        return _Spec()


class _RecordingCheckpoint:
    """Records the kind/prompt of each limit-checkpoint call and returns a
    fixed decision — the unified-framework seam the block must route through."""

    def __init__(self, allow_continue: bool) -> None:
        self._allow = allow_continue
        self.calls: list[dict] = []

    async def __call__(self, *, kind: str, prompt: str, detail: str,
                       extension_amount: float, run_id: str | None = None) -> object:
        self.calls.append({"kind": kind, "prompt": prompt, "detail": detail})
        return type("Decision", (), {"allow_continue": self._allow})()


class _FakeSession:
    def __init__(
        self,
        *,
        block: bool,
        threshold: float,
        non_interactive: bool = False,
        checkpoint_allows: bool = True,
    ) -> None:
        self._config = type("Cfg", (), {
            "cost_warn": CostWarnConfig(
                enabled=True,
                model_threshold_per_1m_input_usd=threshold,
                block_on_high_cost=block,
            ),
        })()
        self._resolver = _FakeResolver()
        self._chat_events = _FakeEventLog()
        self._non_interactive = non_interactive
        self._handle_chat_limit_checkpoint = _RecordingCheckpoint(checkpoint_allows)

    @property
    def checkpoint_calls(self) -> list[dict]:
        return self._handle_chat_limit_checkpoint.calls

    def event_snapshot(self) -> list[tuple[str, dict]]:
        return self._chat_events.snapshot()


def _run(coro):
    return asyncio.run(coro)


def _skip_if_no_pricing() -> None:
    if get_input_cost_per_1m_usd("gpt-4o") is None:
        pytest.skip("gpt-4o not in litellm pricing DB")


# ---------------------------------------------------------------------------

def test_approve_allows_switch_via_unified_framework() -> None:
    """Tier 2: block on + high-cost + approved → True, routed through the
    kind='cost.high_cost_model' checkpoint (not a bespoke path)."""
    _skip_if_no_pricing()
    s = _FakeSession(block=True, threshold=0.0, checkpoint_allows=True)
    allowed = _run(maybe_block_high_cost_model(s, "gpt-4o", action="model_override"))
    assert allowed is True
    assert s.checkpoint_calls, "expected the unified limit-checkpoint to be called"
    assert s.checkpoint_calls[0]["kind"] == "cost.high_cost_model"


def test_decline_blocks_switch() -> None:
    """Tier 2: block on + high-cost + declined → False (switch must not apply)."""
    _skip_if_no_pricing()
    s = _FakeSession(block=True, threshold=0.0, checkpoint_allows=False)
    allowed = _run(maybe_block_high_cost_model(s, "gpt-4o", action="model_override"))
    assert allowed is False


def test_block_disabled_is_warn_only_no_checkpoint() -> None:
    """Tier 2: block_on_high_cost=False → True and the checkpoint is NOT called.

    Falsification: if the gate ignored the opt-in flag, the warn-only default
    (S1–S3) would suddenly start prompting for every high-cost switch.
    """
    _skip_if_no_pricing()
    s = _FakeSession(block=False, threshold=0.0)
    allowed = _run(maybe_block_high_cost_model(s, "gpt-4o", action="model_override"))
    assert allowed is True
    assert s.checkpoint_calls == [], "warn-only must not dispatch a confirm"


def test_non_interactive_fail_closed_no_checkpoint() -> None:
    """Tier 2: non-interactive + block + high-cost → False WITHOUT a confirm.

    Falsification: dispatching a confirm on a non-TTY session would hang; the
    pre-checkpoint guard must deny first. Asserts no checkpoint call + a
    fail-closed block event.
    """
    _skip_if_no_pricing()
    s = _FakeSession(block=True, threshold=0.0, non_interactive=True)
    allowed = _run(maybe_block_high_cost_model(s, "gpt-4o", action="model_override"))
    assert allowed is False
    assert s.checkpoint_calls == [], "non-interactive must not dispatch a confirm"
    reasons = [d.get("reason") for _, d in s.event_snapshot()]
    assert "non_interactive_fail_closed" in reasons


def test_low_cost_model_is_not_gated() -> None:
    """Tier 2: a below-threshold model is never gated (True, no checkpoint)."""
    _skip_if_no_pricing()
    # threshold absurdly high → gpt-4o counts as below-threshold.
    s = _FakeSession(block=True, threshold=10_000.0)
    allowed = _run(maybe_block_high_cost_model(s, "gpt-4o", action="model_override"))
    assert allowed is True
    assert s.checkpoint_calls == [], "a cheap model must not be gated"


def test_unknown_model_is_not_gated() -> None:
    """Tier 2: an unknown model (no pricing) is treated as not-high-cost → True."""
    s = _FakeSession(block=True, threshold=0.0)
    allowed = _run(
        maybe_block_high_cost_model(s, "__no_such_model__", action="model_override")
    )
    assert allowed is True
    assert s.checkpoint_calls == []
