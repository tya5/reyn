"""Tier 2: #3671 follow-up — ``REYN_STALL_TRACE`` (#4405) extended to the
startup path (``run_textual_chat`` / ``TextualChatApp.on_mount``), mirroring
``tests/runtime/test_4405_stall_trace_wiring.py``'s own turn-side pattern
and its own stated reason: prove the WIRING (arm/disarm actually called,
with the right value, at the right points), never the N-second
stall-detection behavior itself (banned by testing policy's duration
rules — see ``stall_trace.py``'s own docstring).

Two disarm sites exist by design (``on_mount()``'s own
``mark_first_frame()`` call — the real, intended boundary — and
``run_textual_chat()``'s ``finally`` — a safety net for "never reached
first frame"), so this file pins BOTH independently: the safety net via
``run_textual_chat`` with a stubbed ``TextualChatApp.run_async`` that
never mounts, and the real boundary via a directly-run, real
``TextualChatApp.on_mount()`` (headless ``run_test()``, the same
technique ``test_loop_probe_3539.py`` uses) — never touching
``run_textual_chat`` itself for that half, since the real disarm site is
inside ``on_mount``, not that function.

``monkeypatch.setattr(stall_trace, "arm"/"disarm", ...)`` only intercepts
because both call sites do a function-local ``from reyn.runtime.
stall_trace import arm/disarm as ...`` (a fresh module-attribute lookup
every call) — the same hoisting trap ``test_4405_stall_trace_wiring.py``
already documents for the turn-side; keep these imports function-local if
you touch either call site.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.runtime import stall_trace
from tests._support.textual_chat_test_helpers import QueueTransport


@pytest.mark.asyncio
async def test_stall_trace_armed_before_app_construction_when_env_set(
    monkeypatch,
) -> None:
    """Tier 2: with REYN_STALL_TRACE set, run_textual_chat() calls
    stall_trace.arm(N) BEFORE mark_app_constructed()/TextualChatApp(...) —
    real function swapped for a recorder, real call observed. run_async
    is stubbed to raise immediately (simulating "never reached first
    frame"), so the finally safety net is what disarms — the OTHER
    disarm site (on_mount) is pinned separately below."""
    monkeypatch.setenv("REYN_STALL_TRACE", "7")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append(f"arm:{seconds}"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_async(self, *args, **kwargs):
        # arm() must have already run by the time run_async is reached.
        assert calls == ["arm:7.0"], "arm() must fire before app.run_async()"
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)

    from reyn.interfaces.inline.textual_chat.app import run_textual_chat

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport())

    assert calls == ["arm:7.0", "disarm"], (
        "the finally safety net must disarm even though on_mount() (the "
        "real boundary) never ran"
    )


@pytest.mark.asyncio
async def test_stall_trace_not_touched_when_env_unset(monkeypatch) -> None:
    """Tier 2: accept-side — with REYN_STALL_TRACE unset (the default),
    neither arm() nor disarm() is called around startup. Proves the
    wiring costs nothing for the overwhelming majority of runs that never
    opt in."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_async(self, *args, **kwargs):
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)

    from reyn.interfaces.inline.textual_chat.app import run_textual_chat

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport())

    assert calls == [], "arm/disarm must not be touched when the env var is unset"


@pytest.mark.asyncio
async def test_stall_trace_disarmed_at_first_frame_via_on_mount(monkeypatch) -> None:
    """Tier 2: the REAL boundary — a real, headless TextualChatApp
    (``run_test()``, matching ``test_loop_probe_3539.py``'s own
    technique) reaching ``on_mount()``'s ``mark_first_frame()`` call
    disarms the trace THERE, not only via run_textual_chat's safety net
    (pinned separately above) — this test never goes through
    run_textual_chat at all, since the site under test here
    (``on_mount``) is reached the same way regardless of which function
    constructed the app.

    **#5870 stage 1**: a SECOND, later ``disarm()`` is now expected too —
    ``_watch_loop_responsiveness``'s own worker (started right after this
    first-frame disarm, see that method's own docstring) holds the SAME
    global timer continuously re-armed for as long as the app runs, and
    disarms it in its own ``finally`` when the app shuts down and the
    worker is cancelled (the assertion below is deliberately OUTSIDE the
    ``async with`` block, so it reads state AFTER that shutdown has
    already happened). Both are real, distinct cleanup events now — the
    first-frame handoff this test is actually about, and the tripwire's
    own worker-exit cleanup — not a regression of the first."""
    monkeypatch.setenv("REYN_STALL_TRACE", "5")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert calls == ["disarm"], (
            "on_mount()'s own mark_first_frame() call must disarm the trace "
            "directly, before the app has even shut down — this is the "
            "real, intended boundary, not just the finally-block safety net"
        )

    assert calls == ["disarm", "disarm"], (
        "expected exactly ONE further disarm() after app shutdown — the "
        "tripwire worker's own finally cleanup (#5870 stage 1) — not zero "
        f"(a dangling timer) or more than one: {calls!r}"
    )


@pytest.mark.asyncio
async def test_the_tripwire_arms_its_own_dead_mans_switch_unconditionally(
    monkeypatch,
) -> None:
    """Tier 2: #5870 stage 1 — right after ``on_mount()``'s own disarm
    above hands the one process-wide timer off, ``_watch_loop_
    responsiveness``'s own worker arms it AGAIN itself, with
    ``repeat=False`` — the per-tick dead-man's switch, not the
    ``REYN_STALL_TRACE`` bracket's own ``repeat=True`` shape. Deliberately
    with ``REYN_STALL_TRACE`` UNSET: this arm must fire regardless, the
    same "arrives unannounced, so it cannot wait for a manual opt-in"
    reasoning ``loop_probe.py``'s own module docstring already states for
    the tripwire itself. Wiring only — no real delay, no threshold
    crossing (banned by testing policy's duration rules, this file's own
    module docstring)."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    calls: "list[tuple[float, bool | None]]" = []
    monkeypatch.setattr(
        stall_trace, "arm",
        lambda seconds, **kw: calls.append((seconds, kw.get("repeat"))),
    )
    monkeypatch.setattr(stall_trace, "disarm", lambda: None)

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()

    assert calls, (
        "the tripwire's own worker never armed the dead-man's switch — "
        "expected it to fire right after on_mount()'s own disarm, with no "
        "REYN_STALL_TRACE opt-in required"
    )
    seconds, repeat = calls[0]
    assert seconds == pytest.approx(0.25), f"expected the 250ms tripwire threshold, got {seconds!r}"
    assert repeat is False, (
        "the tripwire's own arm must use repeat=False (a re-armed "
        "dead-man's switch), not repeat=True (a fixed-cadence alarm)"
    )
