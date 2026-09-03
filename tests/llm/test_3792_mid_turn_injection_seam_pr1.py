"""Tier 2: #3792 PR1 — the mid-turn injection seam's POSITION, zero behavior
change.

architect's design (issue #3792) splits the feature into two PRs because a
half-landed 2-phase peek/pop breaks in BOTH directions (peek-only re-triggers
the same utterance; pop-only loses an utterance on an abnormal exit). PR1
lands only the seam's call site in ``RouterLoop.run_loop`` — a getattr-guarded
host hook, ``peek_mid_turn_injections``, called once per iteration, after the
cancel checkpoint and immediately before whichever send that iteration makes
(force-close or normal). No host implements it for real yet (PR2 wires the
real 2-phase peek/pop); this PR's own production behaviour is a no-op.

What this file pins:

- The seam fires once per iteration when a host implements it (count).
- The seam does NOT fire on a cancelled iteration (position: after the
  cancel checkpoint, so a cancel ``break`` never reaches it).
- The seam fires BEFORE that iteration's LLM send (order).
- A host that does not implement the hook is unaffected (byte-identical) —
  covered implicitly by every OTHER router_loop test in this suite, which
  all use ``FakeRouterHost``/``RouterHostAdapter`` without this hook and
  continue to pass unmodified.
"""
from __future__ import annotations

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeRouterHost, text_result, tool_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


class _OrderRecordingLLM:
    """Real callable (not a mock): scripts responses AND appends "send" to
    the same ``host.call_order`` log the fake's ``peek_mid_turn_injections``
    writes to, so a test can assert the two interleave in the right order."""

    def __init__(self, host: FakeRouterHost, script: list[LLMToolCallResult]) -> None:
        self._host = host
        self._script = list(script)
        self.call_count = 0

    async def __call__(self, **kwargs) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        self._host.call_order.append("send")
        return result


def _loop(host: FakeRouterHost, llm, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(
        host=host, chain_id="chain-3792-pr1", max_iterations=max_iterations,
        llm_caller=llm,
    )


@pytest.mark.asyncio
async def test_seam_fires_once_per_iteration() -> None:
    """Tier 2: #3792 PR1 — a 2-round turn (tool_calls then final text) calls
    the seam exactly once per round: once for the tool-round send, once for
    the terminal send.

    Falsification (performed during review): removing the seam call from
    ``run_loop`` (or moving it inside the ``if _peek_injection_fn is not
    None:`` dead branch some other way) makes this test go RED —
    ``host.mid_turn_injection_peeks`` stays empty.
    """
    host = FakeRouterHost()
    llm = _OrderRecordingLLM(host, [
        tool_result([{"name": "read_file", "args": {"path": "x"}}]),
        text_result("done"),
    ])
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert host.mid_turn_injection_peeks == [0, 1]


@pytest.mark.asyncio
async def test_seam_does_not_fire_on_a_cancelled_iteration() -> None:
    """Tier 2: #3792 PR1 — position witness: the seam sits AFTER the cancel
    checkpoint, so a cancelled iteration ``break``s before ever reaching it.

    Falsification (performed during review): moving the seam call to BEFORE
    the cancel checkpoint makes this test go RED — ``mid_turn_injection_peeks``
    would then have one entry even though the turn cancels on iteration 0.
    """

    class _CancellableHost(FakeRouterHost):
        def _is_turn_cancel_requested(self) -> bool:
            return True  # cancel on every check, including the first

    host = _CancellableHost()
    llm = _OrderRecordingLLM(host, [text_result("should never run")])
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert host.mid_turn_injection_peeks == []
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_seam_fires_before_the_send_each_round() -> None:
    """Tier 2: #3792 PR1 — order witness: within EACH iteration, the seam
    fires before that iteration's LLM send, not after.

    Falsification (performed during review): moving the seam call to AFTER
    the ``_llm(...)`` call in ``run_loop`` makes this test go RED —
    ``host.call_order`` would read ["send", "peek_mid_turn_injections", ...]
    instead.
    """
    host = FakeRouterHost()
    llm = _OrderRecordingLLM(host, [
        tool_result([{"name": "read_file", "args": {"path": "x"}}]),
        text_result("done"),
    ])
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert host.call_order == [
        "peek_mid_turn_injections", "send",
        "peek_mid_turn_injections", "send",
    ]
