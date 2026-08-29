"""Tier 1/2: #5450 — ``LLMStub``'s ``control="gated"`` mode, the mechanism
witnesses (architect's own #5450 design, witnesses ①③④⑦).

Real ``litellm.acompletion`` boundary throughout — ``LLMStub.install()``
patches it for real; no mock. Witness ② (does the REAL driver run, not
just the stub) belongs to a per-file migration (e.g. ``test_2242_hard_
cancel.py``) that also drives a real ``Session``, not this file, which
tests ``LLMStub`` in isolation.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.dev.testing.llm_stub import (
    LLMStub,
    UnknownLLMStubControlError,
)


@pytest.mark.asyncio
async def test_gated_call_hangs_until_release_is_set() -> None:
    """Tier 1: witness ① — a control="gated" call fires call_started
    immediately, then does NOT complete until the test sets release."""
    stub = LLMStub(control="gated")
    stub.install()
    try:
        call_task = asyncio.ensure_future(
            stub._handle("m", [{"role": "user", "content": "hi"}]),
        )
        await stub.call_started.wait()

        # not yet released — the call must still be pending.
        await asyncio.sleep(0)
        assert not call_task.done(), "the gated call completed before release was set"

        stub.release.set()
        response = await call_task
        assert response.choices[0].finish_reason == "stop"
    finally:
        stub.restore()


@pytest.mark.asyncio
async def test_a_cancellation_while_gated_propagates_normally() -> None:
    """Tier 1: witness (mechanism half of the deleted "gated_swallow_
    cancel" question) — reyn's own stub does NOT swallow a
    CancelledError delivered while suspended; it propagates exactly like
    any other await. See LLMStub's own module docstring for the real,
    executed litellm/httpx finding this decision rests on."""
    stub = LLMStub(control="gated")
    stub.install()
    try:
        call_task = asyncio.ensure_future(
            stub._handle("m", [{"role": "user", "content": "hi"}]),
        )
        await stub.call_started.wait()

        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task
    finally:
        stub.restore()


def test_an_unknown_control_value_raises_at_construction() -> None:
    """Tier 1: witness — control= is a CLOSED vocabulary (mirrors #5382's
    closed cause vocabulary); a typo fails LOUD, at construction, not
    silently at call time."""
    with pytest.raises(UnknownLLMStubControlError):
        LLMStub(control="not_a_real_mode")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_control_none_is_unchanged_from_before_5450() -> None:
    """Tier 1: accept-side / noise guard — the default (no control=) keeps
    #5103's original immediate-return behavior, unaffected by #5450's
    addition. The 8 files already migrated under #5103/#5459 depend on
    this staying true."""
    stub = LLMStub()
    stub.install()
    try:
        response = await stub._handle("m", [{"role": "user", "content": "hi"}])
        assert response.choices[0].finish_reason == "stop"
        assert not stub.call_started.is_set(), (
            "control=None must not touch call_started/release at all"
        )
    finally:
        stub.restore()
