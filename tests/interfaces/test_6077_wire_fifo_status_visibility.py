"""Tier 2: #6077 proposal 5 (architect ruling) — the wire FIFO
(``TextualChatApp._drain_wire_fifo``) is a deliberately single-in-flight,
strictly-serial design (its own docstring); this does not fix any delay,
it makes a stuck/slow send LEGIBLE — "reyn is frozen" vs. "a send is in
flight" — by threading two RAW facts onto the status line, the SAME
"prepend, don't replace" sibling shape ``#5732``'s ``frame pump: N
swallowed`` segment already established:

  ① ``wire_fifo_waiting``  — ``self._wire_fifo.qsize()`` directly, the
     EXISTING ``asyncio.Queue`` count (no new counter).
  ② ``wire_fifo_inflight_elapsed`` — seconds since the currently-running
     unit was taken off the queue, off the SAME injectable ``self._clock``
     the tool-elapsed timer already uses (no new clock), ``None`` when
     nothing is in flight.

No threshold anywhere (architect ruling, #6237-shaped trap avoided): the
segment renders for the ENTIRE duration a unit is in flight, never gated
on an elapsed cutoff — this file asserts presence/absence and the two raw
numbers, never a "slow" judgement.

No mocks: real ``PumpSwallowStats``-sibling-free wiring, real
``TextualChatApp`` + ``run_test()`` pilot (mirrors
``tests/interfaces/test_5732_pump_swallow_visibility.py``'s own end-to-end
idiom), a real injected fake clock (never ``time.monotonic()`` and never
``sleep`` — CLAUDE.md: "the clock is an input you supply, never a sleep
you wait out"), and the status line's own PUBLIC rendered text (never a
private ``_wire_fifo_inflight_started_at`` read) as the only witness.

Elapsed-time advance is driven by directly invoking the App's own PUBLIC
``on_timer`` with a genuine ``events.Timer(timer=app.pump_heartbeat_timer,
...)`` — the exact idiom ``tests/interfaces/test_loop_probe_3539.py``
already uses to drive the heartbeat deterministically without a real 1s
wait — which exercises the SAME "heartbeat also refreshes the wire
segment while something is in flight" wiring this PR adds to
``on_timer``, not a bypass of it.

Strip-falsify (in-file ``Edit`` only, on ``chrome.status_line_text``'s
``if wire_fifo_inflight_elapsed is not None: text = f"wire: ..."``
prepend — removed, then restored, no ``git checkout``/``stash``): all four
PRESENT-witness tests in this file went RED, each with the SAME shape —
the segment simply absent from the returned text. Verbatim observed
failures:

- ``test_status_line_prepends_wire_segment_when_inflight_elapsed_is_set``:
  ``AssertionError: opus · alpha    $0.0000  —`` /
  ``assert False`` / ``where False = <built-in method startswith of str
  object at 0x111904730>('wire: 2 waiting, sending 4.2s')``
- ``test_status_line_wire_segment_coexists_with_pump_swallow_segment``:
  ``AssertionError: assert 'wire: 0 waiting, sending 0.1s' in 'frame
  pump: 1 swallowed — see log · opus · alpha    $0.0000  —'``
- ``test_status_line_wire_segment_renders_even_at_zero_elapsed``:
  ``AssertionError: opus · alpha    $0.0000  —`` / ``assert 'wire: 0
  waiting, sending 0.0s' in 'opus · alpha    $0.0000  —'``
- ``test_wire_fifo_segment_shows_waiting_count_and_injected_clock_elapsed``:
  ``AssertionError: opus · default    $0.0000  —`` / ``assert 'wire: 0
  waiting, sending 0.0s' in 'opus · default    $0.0000  —'``

The two DENY-witness tests stayed green throughout the strip (expected —
they assert ABSENCE, which a stripped segment trivially still satisfies).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import status_line_text
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

# ---------------------------------------------------------------------------
# status_line_text — pure-function level
# ---------------------------------------------------------------------------


def test_status_line_unaffected_when_wire_fifo_inflight_elapsed_is_none() -> None:
    """Tier 2: deny witness (pure-function level) — the default
    (``wire_fifo_inflight_elapsed=None``) is byte-identical to the
    pre-#6077 text, and never mentions ``qsize`` leftovers via
    ``wire_fifo_waiting`` alone (nonzero waiting with no in-flight unit is
    not a state this design can reach, but the function must still not
    render on ``waiting`` alone)."""
    text_before = status_line_text({"model_active_class": "opus"}, "alpha")
    text_default = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        wire_fifo_waiting=3, wire_fifo_inflight_elapsed=None,
    )
    assert text_before == text_default
    assert "wire:" not in text_default


def test_status_line_prepends_wire_segment_when_inflight_elapsed_is_set() -> None:
    """Tier 2: present witness (pure-function level) — both raw facts
    (waiting count, elapsed seconds) appear, no threshold, no judgement
    word ("slow"/"stuck") anywhere in the segment."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        wire_fifo_waiting=2, wire_fifo_inflight_elapsed=4.2,
    )
    assert text.startswith("wire: 2 waiting, sending 4.2s"), text
    assert "opus" in text, "the ordinary status text must still follow"
    for judgement_word in ("slow", "stuck", "frozen", "stall"):
        assert judgement_word not in text.lower(), (
            f"the segment must state raw facts only, found {judgement_word!r} in {text!r}"
        )


def test_status_line_wire_segment_coexists_with_pump_swallow_segment() -> None:
    """Tier 2: two independently-gated sibling segments (#5732's
    ``pump_swallow_count``, #6077's wire segment) must not silently
    swallow each other when both are nonzero at once."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        pump_swallow_count=1, wire_fifo_waiting=0, wire_fifo_inflight_elapsed=0.1,
    )
    assert "frame pump: 1 swallowed" in text
    assert "wire: 0 waiting, sending 0.1s" in text


def test_status_line_wire_segment_renders_even_at_zero_elapsed() -> None:
    """Tier 2: NO THRESHOLD acceptance — a freshly-started unit
    (``elapsed=0.0``, the instant it was taken off the queue) still
    renders. A hidden ``if elapsed > N`` cutoff would fail this."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        wire_fifo_waiting=0, wire_fifo_inflight_elapsed=0.0,
    )
    assert "wire: 0 waiting, sending 0.0s" in text, text


# ---------------------------------------------------------------------------
# App-level — real TextualChatApp, real run_test() pilot, an injected fake
# clock (no sleep), a real asyncio.Event-gated unit (no sleep) driving the
# wire FIFO.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A deterministic, test-driven stand-in for ``time.monotonic`` —
    starts at 0.0, advances only when the test calls :meth:`advance`.
    Mirrors the injectable-clock idiom ``TextualChatApp.__init__``'s own
    ``clock`` parameter already documents (the tool-elapsed timer)."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _StubTransport(ClientTransportStub):
    """Mirrors test_5732's own ``_StubTransport`` — no attach machinery
    needed, just an app that mounts and stays running with a ``frames()``
    stream that never itself produces anything (the wire FIFO here is
    driven directly via ``_enqueue_wire``, not through incoming frames)."""

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def has_session(self) -> bool:
        return True

    def attach_failed(self) -> bool:
        return False

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        return
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
        return False

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _StubReadModel(ChatReadModel):
    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return {"model_active_class": "opus"}

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self):
        from pathlib import Path

        return Path("/tmp/reyn_6077_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


@pytest.mark.asyncio
async def test_wire_fifo_segment_absent_when_nothing_was_ever_enqueued() -> None:
    """Tier 2: deny witness, end to end — an app that mounts and never
    submits anything shows no ``wire:`` segment on its status line (the
    always-visible, always-there segment #6077's own acceptance forbids)."""
    transport = _StubTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        status_text = str(app.query_one(StatusLine).render())
        assert "wire:" not in status_text, status_text


@pytest.mark.asyncio
async def test_wire_fifo_segment_shows_waiting_count_and_injected_clock_elapsed() -> None:
    """Tier 2: present witness, end to end — acceptance ①②③ together:
    ⑴ no threshold (renders at elapsed=0.0, i.e. the instant a unit starts)
    ⑵ ``qsize()`` reused directly, not a new counter — two further units
       queued BEHIND the in-flight one show as "2 waiting"
    ⑶ elapsed advances off the injected clock ONLY — no sleep anywhere in
       this test; the clock is stepped, then the App's own public
       ``on_timer`` is invoked with its own real heartbeat timer (mirrors
       ``test_loop_probe_3539.py``'s own idiom) to trigger the refresh
       this PR wires onto the heartbeat specifically for the "nothing else
       will re-render while stuck" case.
    """
    from textual import events

    clock = _FakeClock()
    transport = _StubTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel(), clock=clock)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_unit() -> None:
        started.set()
        await release.wait()

    async def _noop_unit() -> None:
        return None

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # ① renders at elapsed 0.0 — no threshold. _drain_wire_fifo's own
        # start-of-unit refresh (this PR) draws it immediately — no
        # waiting for a frame or a heartbeat tick.
        app._enqueue_wire(_slow_unit())
        await started.wait()
        await pilot.pause()
        status_text = str(app.query_one(StatusLine).render())
        assert "wire: 0 waiting, sending 0.0s" in status_text, status_text

        # ② two further units queue BEHIND the in-flight one — qsize()
        # reused directly, no new counter.
        app._enqueue_wire(_noop_unit())
        app._enqueue_wire(_noop_unit())
        await pilot.pause()

        # ③ elapsed advances off the injected clock, surfaced via the
        # PUBLIC on_timer + the App's own real heartbeat timer identity.
        clock.advance(4.2)
        assert app.pump_heartbeat_timer is not None
        await app.on_timer(
            events.Timer(timer=app.pump_heartbeat_timer, time=0.0, count=1),
        )
        status_text = str(app.query_one(StatusLine).render())
        assert "wire: 2 waiting, sending 4.2s" in status_text, status_text

        # Release the in-flight unit; the two queued no-ops drain behind
        # it; the segment must then disappear (deny again, post-drain).
        release.set()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        status_text = str(app.query_one(StatusLine).render())
        assert "wire:" not in status_text, status_text
