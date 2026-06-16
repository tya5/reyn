"""Tier 2: #1666 — per-turn tool_call count cap (cost-bound, OS-level).

A degenerate (weak-model, long-context) completion can emit thousands of
``tool_calls`` (observed 3451 in one SWE-bench completion) → thousands of
tool-result messages → context/cost blowup. ``RouterLoop._enforce_tool_call_cap``
bounds the count at ``safety.loop.max_tool_calls_per_turn`` by TRUNCATING the
overflow off ``result.tool_calls`` in place **before** interpret, so every
downstream branch + the assistant↔tool-result alignment inherit the bound from a
single choke point. The P6 ``tool_call_cap_exceeded`` event records the original
*attempted* count (history is bounded to ``kept``; the true magnitude survives in
the audit log), and a single re-grounding notice is appended after the round.

No mocks: a real ``LLMToolCallResult`` completion + a real recording events sink.
The cap method is exercised directly on a minimally-constructed ``RouterLoop``
(the loop wiring around it is straightforward message-append, covered by the
config + driver threading + the notice-content assertions here).
"""
from __future__ import annotations

from reyn.chat.router_loop import RouterLoop
from reyn.llm import TokenUsage
from reyn.llm.llm import LLMToolCallResult


class _RecordingEvents:
    """Real fake events sink; records emitted events (no Mock)."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append({"kind": kind, **kwargs})


class _MinimalHost:
    def __init__(self) -> None:
        self.events = _RecordingEvents()


class _CapLoop(RouterLoop):
    """RouterLoop with just the state ``_enforce_tool_call_cap`` /
    ``_tool_call_cap_notice`` read — skips the real __init__ (the cap method only
    needs ``host.events``, ``chain_id``, and ``_max_tool_calls_per_turn``)."""

    def __init__(self, cap: int) -> None:
        self.host = _MinimalHost()  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self._max_tool_calls_per_turn = cap


def _make_result(n: int) -> LLMToolCallResult:
    """A real completion carrying *n* distinct tool_calls (varying args, so dedup
    would NOT collapse them — mirrors the #1666 repro)."""
    tcs = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "list_actions", "arguments": f'{{"q": "{i}"}}'},
        }
        for i in range(n)
    ]
    return LLMToolCallResult(
        content="", tool_calls=tcs, finish_reason="tool_calls", usage=TokenUsage(),
    )


def test_cap_truncates_overflow_in_place() -> None:
    """Tier 2: #1666 — a completion with > cap tool_calls is truncated IN PLACE to
    the first cap; the overflow is dropped before interpret (so it is never
    executed nor appended). Returns (attempted, kept)."""
    loop = _CapLoop(cap=50)
    result = _make_result(120)

    info = loop._enforce_tool_call_cap(result)

    assert info == (120, 50)
    # The KEPT calls are exactly the first 50 (index 0..49), order preserved →
    # alignment intact; the overflow (call_50+) is gone.
    assert result.tool_calls[0]["id"] == "call_0"
    assert result.tool_calls[-1]["id"] == "call_49"
    assert all(tc["id"] != "call_50" for tc in result.tool_calls), "overflow dropped"


def test_cap_emits_event_with_original_attempted_count() -> None:
    """Tier 2: #1666 — the P6 tool_call_cap_exceeded event records the ORIGINAL
    attempted count (3451-style magnitude survives in the audit log even though
    history is bounded to kept). Audit-fidelity requirement (lead)."""
    loop = _CapLoop(cap=50)
    loop._enforce_tool_call_cap(_make_result(3451))

    # Exactly one cap event recorded (the sink saw nothing else this turn).
    assert [e["kind"] for e in loop.host.events.emitted] == ["tool_call_cap_exceeded"]
    event = loop.host.events.emitted[0]
    assert event["attempted"] == 3451, "true attempted count must survive in audit"
    assert event["kept"] == 50


def test_cap_no_op_at_or_below_limit() -> None:
    """Tier 2: #1666 — a completion at/below the cap is untouched: no truncation,
    no event, returns None (the common case must be a pure pass-through)."""
    loop = _CapLoop(cap=50)
    result = _make_result(50)

    info = loop._enforce_tool_call_cap(result)

    assert info is None
    # All 50 retained (index 49 = the 50th) — at-limit is not truncated.
    assert result.tool_calls[-1]["id"] == "call_49"
    assert not loop.host.events.emitted, "no event when the cap does not fire"


def test_cap_zero_means_unlimited() -> None:
    """Tier 2: #1666 — cap=0 disables the bound (matches the sibling loop-cap
    convention): a huge batch passes through untouched, no event."""
    loop = _CapLoop(cap=0)
    result = _make_result(5000)

    info = loop._enforce_tool_call_cap(result)

    assert info is None
    # The full batch passes through untouched (last = index 4999 = the 5000th).
    assert result.tool_calls[-1]["id"] == "call_4999"
    assert not loop.host.events.emitted


def test_cap_notice_states_attempted_and_kept() -> None:
    """Tier 2: #1666 — the re-grounding notice is decision-enabling: it states the
    true attempted count, the cap, and what to do (call fewer)."""
    loop = _CapLoop(cap=50)
    msg = loop._tool_call_cap_notice(3451, 50)

    assert msg["role"] == "user"
    body = msg["content"]
    assert "3451" in body and "50" in body
    assert "fewer" in body.lower()


def test_cap_default_is_50_in_config() -> None:
    """Tier 2: #1666 — the default safety.loop.max_tool_calls_per_turn is 50
    (the confirmed cost-bound default; ~70x below the runaway, headroom over
    legitimate parallel use)."""
    from reyn.config import LoopConfig

    assert LoopConfig().max_tool_calls_per_turn == 50
