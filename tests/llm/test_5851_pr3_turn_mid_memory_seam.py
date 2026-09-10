"""Tier 2: #5851 PR-3 — the turn-mid memory mini-ladder's router_loop.py
wiring: the iteration-boundary poll (``_check_turn_mid_memory``), its
mandatory ordering relative to the existing cancel checkpoint (architect
ruling, #5939 issue thread: ``1. cancel → 2. compact(await) → 3.
cancel-recheck → 4. LLM call``), and ``turn_stopped_memory``'s own distinct
recording from ``turn_cancelled``.

Only the router_loop.py SEAM is pinned here (no real ``Session`` — see
``tests/runtime/test_5851_pr3_turn_mid_memory_ladder.py`` for
``Session._check_turn_mid_memory_ladder``'s own ladder logic). Driven via
``RouterLoop.run_loop`` + a ``FakeRouterHost`` subclass exposing
``_check_turn_mid_memory``/``_is_turn_cancel_requested`` directly — no
mocks, matching ``test_turn_cancel_1468.py``'s own established harness for
this exact seam.
"""
from __future__ import annotations

import pytest

from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeRouterHost, text_result
from tests._support.router_loop import ScriptedLLM as _ScriptedLLM


class _MemoryHost(FakeRouterHost):
    """FakeRouterHost subclass exposing both iteration-boundary hooks so a
    test can drive them independently — same shape as
    ``test_turn_cancel_1468.py``'s own ``_CancellableHost``."""

    def __init__(self) -> None:
        super().__init__()
        self.memory_check_calls: int = 0
        self._stop_after_n: "int | None" = None
        # A side effect the memory check itself can trigger — simulates
        # "a cancel arrived WHILE the ladder's own await (a real
        # compaction LLM call) was in flight".
        self._cancel_during_memory_check: bool = False
        self._cancel_requested: bool = False

    def arm_stop_after(self, n: int) -> None:
        """Return a stop reason from the (n+1)th call onward (0 = first call)."""
        self._stop_after_n = n

    def arm_cancel_during_memory_check(self) -> None:
        self._cancel_during_memory_check = True

    async def _check_turn_mid_memory(self) -> "str | None":
        self.memory_check_calls += 1
        if self._cancel_during_memory_check:
            self._cancel_requested = True
        if self._stop_after_n is None:
            return None
        return "turn_stopped_memory" if self.memory_check_calls > self._stop_after_n else None

    def _is_turn_cancel_requested(self) -> bool:
        return self._cancel_requested


def _loop(host: _MemoryHost, llm: _ScriptedLLM, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(
        host=host, chain_id="chain-memory-test", max_iterations=max_iterations,
        llm_caller=llm,
    )


# ── 1. The stop itself ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_stop_breaks_the_loop_cleanly() -> None:
    """Tier 2: a non-None return from ``_check_turn_mid_memory`` breaks the
    loop without raising, before the LLM call that iteration would have
    made."""
    host = _MemoryHost()
    host.arm_stop_after(0)  # stop on the FIRST check
    llm = _ScriptedLLM([text_result("should not run")])
    await _loop(host, llm).run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_memory_stop_is_recorded_distinctly_from_turn_cancelled() -> None:
    """Tier 2: #5851 acceptance ("その停止が turn_cancelled ではない理由で
    記録される") — a memory-triggered stop must never be recorded, on the
    outbox or in persisted history, as a ``turn_cancelled``/user-cancel
    outcome, and must never itself emit a ``turn_cancelled`` audit event."""
    host = _MemoryHost()
    host.arm_stop_after(0)
    llm = _ScriptedLLM([text_result("unreached")])
    await _loop(host, llm).run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    # Outbox: distinct text, no "interrupted" (the cancel-only wording).
    assert not any("interrupted" in m["text"].lower() for m in host.outbox)
    memory_msgs = [m for m in host.outbox if "memory" in m["text"].lower()]
    assert memory_msgs, f"expected a memory-stop ack on the outbox, got {host.outbox}"
    # Persisted history: distinct meta.kind, never "turn_cancelled".
    kinds = [h["meta"].get("kind") for h in host.history if h.get("meta")]
    assert "turn_stopped_memory" in kinds
    assert "turn_cancelled" not in kinds
    # No turn_cancelled audit event was emitted for this stop.
    assert not any(e.get("type") == "turn_cancelled" for e in host.events.emitted)


# ── 2. Ordering: cancel checked FIRST, memory check never reached ────────


@pytest.mark.asyncio
async def test_an_already_requested_cancel_short_circuits_before_the_memory_check() -> None:
    """Tier 2: architect ruling, mandatory ordering step 1 — an
    already-latched cancel is checked BEFORE the memory ladder; the
    memory check must never even run once cancel has already broken the
    loop this iteration."""
    host = _MemoryHost()
    host._cancel_requested = True
    host.arm_stop_after(0)  # would stop on first call, if ever reached
    llm = _ScriptedLLM([text_result("unreached")])
    await _loop(host, llm).run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert host.memory_check_calls == 0
    assert any(e.get("type") == "turn_cancelled" for e in host.events.emitted)


# ── 3. Ordering: cancel RE-CHECKED after the memory-check await ──────────


@pytest.mark.asyncio
async def test_a_cancel_arriving_during_the_memory_check_is_caught_before_the_llm_call() -> None:
    """Tier 2: architect ruling, mandatory ordering step 3 ("3を落とすと
    圧縮中の cancel が1反復無視される") — a cancel that becomes true AS A
    SIDE EFFECT of the (awaited) memory check — simulating one that
    arrived while a real compaction LLM call was in flight — must still
    stop the turn at THIS boundary, before the turn's own next LLM call,
    never one iteration later.

    Strip witness: removing router_loop.py's own re-check (the second
    ``if callable(_cancel_fn) and _cancel_fn():`` right after the memory
    poll) makes this test RED — the scripted LLM gets called once despite
    the cancel, spending the exact unwanted call step 3 exists to
    prevent."""
    host = _MemoryHost()
    host.arm_cancel_during_memory_check()
    # arm_stop_after left unset -> the memory check itself never asks to
    # stop; only its cancel SIDE EFFECT should end the turn.
    llm = _ScriptedLLM([text_result("must not run")])
    await _loop(host, llm).run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert llm.call_count == 0
    assert host.memory_check_calls == 1
    assert any(e.get("type") == "turn_cancelled" for e in host.events.emitted)
    assert not any(e.get("type") == "turn_stopped_memory" for e in host.events.emitted)


# ── 4. Inert default: no hook, no cost ────────────────────────────────────


@pytest.mark.asyncio
async def test_host_without_the_hook_runs_normally() -> None:
    """Tier 2: a host that does NOT implement ``_check_turn_mid_memory``
    (e.g. a phase host, or an operator who never opted the guard in) runs
    normally — the getattr-guard makes this byte-identical to before
    PR-3 existed. Matches ``test_turn_cancel_1468.py``'s own
    ``test_host_without_cancel_method_runs_normally``."""
    host = FakeRouterHost()  # no _check_turn_mid_memory at all
    llm = _ScriptedLLM([text_result("normal reply")])
    await RouterLoop(
        host=host, chain_id="chain-no-hook", max_iterations=5, llm_caller=llm,
    ).run_loop(messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False)
    assert llm.call_count == 1
    assert not any(e.get("type") == "turn_stopped_memory" for e in host.events.emitted)
