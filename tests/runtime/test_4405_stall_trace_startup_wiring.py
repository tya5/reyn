"""Tier 2: #4405's startup extension — ``reyn chat``'s entrypoint arms/
disarms the stall-trace diagnostic around the WHOLE session, not just
per-turn.

#4406 closed the "no way to see where a turn is blocked" gap for turns
(``Session._run_router_loop``). The owner's own startup — the ~9s span
before the first turn ever runs — had the SAME gap: ``REYN_STALL_TRACE``
only bracketed turns, so a stall during startup (registry construction,
session setup, TUI mount) produced no stack at all.

``chat.py``'s ``_run_bracketed_by_stall_trace`` closes it: arms once
around the WHOLE session (``run_async(_main_chat())``), so a turn's own
arm/disarm nests safely inside it via ``stall_trace``'s depth counter
rather than fighting over the single global ``faulthandler`` timer.

Real ``stall_trace.arm``/``disarm`` functions, monkeypatched to plain
recorder functions (not ``unittest.mock``) — the same #4408 pattern. No
waiting, no sleeping: the point under test is wiring (does the env var
reaching this entrypoint cause arm-then-disarm to bracket the callable),
not the N-second stall-detection behavior itself.
"""
from __future__ import annotations

from reyn.interfaces.cli.commands import chat as chat_cmd
from reyn.runtime import stall_trace


def test_stall_trace_brackets_the_callable_when_env_set(monkeypatch) -> None:
    """Tier 2: with REYN_STALL_TRACE set, arm(N) fires before the wrapped
    callable runs and disarm() fires after — exactly once each."""
    monkeypatch.setenv("REYN_STALL_TRACE", "5")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append(f"arm:{seconds}"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    ran: list[bool] = []

    def _fn() -> None:
        assert calls == ["arm:5.0"], "arm() must fire BEFORE the callable runs, not after"
        ran.append(True)

    chat_cmd._run_bracketed_by_stall_trace(_fn)

    assert ran == [True], "the wrapped callable must actually run"
    assert calls == ["arm:5.0", "disarm"], (
        "expected exactly one arm() then one disarm() bracketing the callable"
    )


def test_stall_trace_not_touched_when_env_unset(monkeypatch) -> None:
    """Tier 2: accept-side — with REYN_STALL_TRACE unset (the default),
    neither arm() nor disarm() is called; the callable still runs."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    ran: list[bool] = []
    chat_cmd._run_bracketed_by_stall_trace(lambda: ran.append(True))

    assert ran == [True]
    assert calls == [], "arm/disarm must not be touched when the env var is unset"


def test_stall_trace_disarmed_even_when_the_callable_raises(monkeypatch) -> None:
    """Tier 2: disarm() fires on the EXCEPTION path too — the try/finally's
    own reason for existing. Without this, a startup that crashes leaves
    the timer armed past process cleanup, dumping unrelated stacks later."""
    monkeypatch.setenv("REYN_STALL_TRACE", "5")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    def _raising() -> None:
        raise RuntimeError("simulated startup failure")

    try:
        chat_cmd._run_bracketed_by_stall_trace(_raising)
    except RuntimeError:
        pass

    assert calls == ["arm", "disarm"], "disarm() must still fire after the callable raised"


def test_nested_arm_disarm_touches_the_real_faulthandler_api_only_at_the_edges(
    monkeypatch,
) -> None:
    """Tier 2: stall_trace's own nesting contract (not chat.py's wiring) —
    an inner arm()/disarm() pair (a "turn" nested inside the "startup"
    bracket) must NOT touch the real faulthandler API at all; only the
    OUTERMOST arm and OUTERMOST disarm may. Observed at the real boundary
    stall_trace wraps (faulthandler.dump_traceback_later /
    cancel_dump_traceback_later, monkeypatched to recorders) — not
    stall_trace's own private ``_depth`` counter, which stays unread by
    this test."""
    import faulthandler

    # Force depth to 0 regardless of any prior test's balance, WITHOUT
    # reading the private counter — disarm() is documented idempotent and
    # clamped at 0, so a few defensive calls (bounded, not a wait) are
    # enough to guarantee a clean starting point through the public API
    # alone.
    for _ in range(4):
        stall_trace.disarm()

    calls: list[str] = []
    monkeypatch.setattr(
        faulthandler, "dump_traceback_later",
        lambda *a, **kw: calls.append("dump_traceback_later"),
    )
    monkeypatch.setattr(
        faulthandler, "cancel_dump_traceback_later",
        lambda: calls.append("cancel_dump_traceback_later"),
    )

    stall_trace.arm(9999)  # outer: the "startup" bracket — must touch the API
    assert calls == ["dump_traceback_later"]

    stall_trace.arm(9999)  # inner: a "turn" nested inside it — must NOT re-touch
    assert calls == ["dump_traceback_later"], "a nested arm() must not re-arm the real timer"

    stall_trace.disarm()  # inner disarm — must NOT cancel (outer still wants it armed)
    assert calls == ["dump_traceback_later"], (
        "a nested disarm() must not cancel the real timer while an outer caller "
        "still holds it armed"
    )

    stall_trace.disarm()  # outer disarm — now depth reaches 0, real cancel
    assert calls == ["dump_traceback_later", "cancel_dump_traceback_later"]
