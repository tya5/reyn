"""Phase ② gates (#3283): a LIVE spinner + elapsed body on RUNNING tool rows,
settling into a COALESCED ``tool(args)`` + ``⎿ result`` block on completion.

A ``tool_call_started`` entry that is in flight grows a live BODY — a braille
spinner + an app-computed ``elapsed Ns`` under its ``tool(args)`` header — driven
by a per-entry, viewport-gated ``FlowView.animate_entry``. On completion the row
SETTLES IN PLACE: the per-entry animation is stopped and the result is folded
into the SAME entry as a ``⎿ <result>`` sub-line (a call and its result are ONE
row, CC's ``⏺ tool(args)`` + ``⎿ result`` block), not a second stacked entry.
These pin the architect-specified ② gates + the owner's coalesce request:

- **live indicator, non-vacuous** (Tier 1): a RUNNING tool body CHANGES as its
  monotonic clock advances (spinner frame + elapsed count); a FROZEN clock leaves
  it static — so the positive assertion is load-bearing, not tautological.
- **settle on completion** (Tier 1 + Tier 2b): the RUNNING marker is stripped and
  the result folded in; the presenter renders the STATIC ``tool(args)`` +
  ``⎿ result`` body; the mounted app does this on RUNNING→SUCCESS/ERROR.
- **coalesce into one entry** (Tier 2b): a correlated started+completed/failed
  pair produces exactly ONE model entry carrying both the call and the ``⎿``
  result; an UNCORRELATED result (no matching op_id) still appends its own entry.
- **drives the real mechanism / no leak** (Tier 2b): the app's ``animate_entry``
  re-presents the RUNNING body live while on screen, and ``settle``
  (``stop_entry_animation``) halts it — the settled row is not re-presented again
  (no leaked timer). Off-screen RUNNING tools are auto-paused (viewport gating).
- **no regression** (Tier 2b): ①'s gutter blink still animates; a plain
  (non-tool) row is unaffected.

All use real instances — a concrete ``QueueTransport`` / ``ScriptedTransport``, a
real mounted :class:`TextualChatApp`, real :class:`OutboxMessage`, a real
list-backed clock, and a real counting :class:`ReynPresenter` SUBCLASS (not a
mock) — per the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from rich.console import Console
from textual_flowview import Entry, EntryState, FlowModel, FlowView

from reyn.interfaces.inline.textual_chat import ReynPresenter, TextualChatApp
from reyn.interfaces.inline.textual_chat.presenter import (
    _RESULT_KIND_KEY,
    _RUNNING_SINCE_KEY,
    _running_indicator,
    _tool_head,
)
from reyn.interfaces.repl.renderer import _SPINNER
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


def _started(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started", text=tool, meta={"tool": tool, "op_id": op_id, "args": {}}
    )


def _completed(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="",
        meta={"tool": tool, "op_id": op_id, "result": {"op": tool, "count": 3}},
    )


def _failed(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_failed",
        text=tool,
        meta={"tool": tool, "op_id": op_id, "error_kind": "Boom", "error_message": "it broke"},
    )


def _running_item(op_id: str, since: float, tool: str = "grep") -> OutboxMessage:
    """A ``tool_call_started`` frame carrying the live-indicator START marker — the
    exact item shape the app stamps when the entry goes RUNNING."""
    return OutboxMessage(
        kind="tool_call_started",
        text=tool,
        meta={"tool": tool, "op_id": op_id, "args": {}, _RUNNING_SINCE_KEY: since},
    )


def _render(renderable, width: int = 80) -> str:
    console = Console(width=width)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


class ScriptedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` replaying a fixed frame list.

    ``end=False`` keeps the stream open after the script so the app under test
    stays mounted for inspection."""

    def __init__(self, messages: "list[OutboxMessage]", *, end: bool = False) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
        else:
            await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class QueueTransport(ClientTransportStub):
    """A real :class:`ClientTransport` fed one frame at a time from a queue, so a
    test can push a ``started`` frame, inspect the RUNNING row, THEN push the
    completion and inspect the settle — with the stream staying open in between."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[OutboxMessage]" = asyncio.Queue()
        self.submitted: list[str] = []

    async def push(self, msg: OutboxMessage) -> None:
        await self._queue.put(msg)

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            msg = await self._queue.get()
            yield DisplayFrame(msg)

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _CountingPresenter(ReynPresenter):
    """A real :class:`ReynPresenter` that also counts how many times each tool
    row is presented, keyed by ``op_id`` — a REAL subclass (not a mock) so the
    genuine body construction still runs; the count witnesses the per-entry
    ``animate_entry`` re-presenting the RUNNING body (and stopping on settle)."""

    def __init__(self) -> None:
        super().__init__()
        self.present_counts: "dict[object, int]" = {}

    async def present(self, entry: "Entry[OutboxMessage]", width: int):
        item = entry.item
        if item.kind == "tool_call_started":
            op = (item.meta or {}).get("op_id")
            self.present_counts[op] = self.present_counts.get(op, 0) + 1
        return await super().present(entry, width)


def _fast_app(**kwargs) -> TextualChatApp:
    """A real app with the animation clocks raised so a body re-present lands
    inside a short test pause. Only the CADENCE differs from production — the
    mechanism (gutter ``animation_fps`` refresh backstop + per-entry
    ``animate_entry``) is identical."""
    app = TextualChatApp(**kwargs)
    app.ANIMATION_FPS = 20.0
    app.RUNNING_BODY_FPS = 20.0
    return app


def _entry_by_kind(app: TextualChatApp, kind: str):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == kind]


# ── Gate 1: live indicator, non-vacuous ───────────────────────────────────────

def test_running_tool_body_advances_as_the_clock_advances() -> None:
    """Tier 1: a RUNNING tool body CHANGES as its monotonic clock advances.

    The live-indicator non-vacuity witness (#3283 ②): the presenter renders the
    spinner frame + elapsed off a clock it re-reads on each ``animate_entry``
    tick, so successive reads at different times produce a DIFFERENT body. Drives
    the REAL presenter with a real list-backed clock (no mock).

    Non-vacuous by construction: the paired
    ``test_frozen_clock_leaves_a_static_running_body`` freezes the clock and the
    body then does NOT change — so this positive assertion is load-bearing."""
    now = [1000.0]
    presenter = ReynPresenter(clock=lambda: now[0])
    entry = FlowModel().append(_running_item("op-live", since=1000.0))

    async def go() -> None:
        now[0] = 1002.0
        a = await presenter.present(entry, 80)
        now[0] = 1007.0
        b = await presenter.present(entry, 80)
        # Fixed height across ticks (no reflow): header row + the indicator row.
        assert a.height == b.height == 2
        ta, tb = _render(a.renderable), _render(b.renderable)
        assert ta != tb, f"running body did not advance across ticks: {ta!r}"
        # Both are genuine live bodies: the elapsed count grew and a spinner frame
        # is present.
        assert "elapsed 2s" in ta and "elapsed 7s" in tb
        assert any(frame in ta for frame in _SPINNER)

    asyncio.run(go())


def test_frozen_clock_leaves_a_static_running_body() -> None:
    """Tier 1: a FROZEN clock leaves the RUNNING body STATIC (the ② non-vacuity
    strip). Reading the same instant twice yields an identical body — the paired
    positive test proves it moves when the clock advances, so the animation is the
    only thing driving the change (not incidental noise)."""
    presenter = ReynPresenter(clock=lambda: 500.0)
    entry = FlowModel().append(_running_item("op-frozen", since=490.0))

    async def go() -> None:
        a = await presenter.present(entry, 80)
        b = await presenter.present(entry, 80)
        assert _render(a.renderable) == _render(b.renderable), "frozen clock must not animate"

    asyncio.run(go())


def test_running_indicator_line_carries_spinner_and_elapsed() -> None:
    """Tier 1: the indicator line is a spinner frame + ``elapsed Ns`` computed from
    the RUNNING-start marker (``now - _running_since``), clamped at 0. Pins
    :func:`_running_indicator` directly (a pure function of item + now)."""
    item = _running_item("op-i", since=100.0)
    line = _running_indicator(item, now=104.0)
    text = line.plain
    assert "elapsed 4s" in text
    assert text[0] in _SPINNER
    # A clock skew (now before since) never renders a negative age.
    assert "elapsed 0s" in _running_indicator(item, now=99.0).plain


# ── Gate 2: settle on completion (static, coalesced body) ─────────────────────

def test_settled_tool_body_is_static_and_matches_the_header() -> None:
    """Tier 1: a marker-free ``tool_call_started`` item (a pre-RUNNING / restored
    row) renders the STATIC ``tool(args)`` header only — clock-invariant, no
    spinner. Presented at two DIFFERENT clock readings the body is identical."""
    now = [10.0]
    presenter = ReynPresenter(clock=lambda: now[0])
    settled = FlowModel().append(_started("op-settled"))  # no _RUNNING_SINCE_KEY marker

    async def go() -> None:
        a = await presenter.present(settled, 80)
        now[0] = 9999.0
        b = await presenter.present(settled, 80)
        assert _render(a.renderable) == _render(b.renderable), "settled body must be static"
        assert a.height == 1  # header only, no indicator row
        assert _render(a.renderable).strip() == _tool_head(settled.item).plain

    asyncio.run(go())


@pytest.mark.asyncio
async def test_app_marks_running_body_live_then_coalesces_on_success() -> None:
    """Tier 2b: the mounted app stamps the RUNNING-start marker on a started tool
    row (making its body live) and, on completion, SETTLES it — stripping the
    marker, folding the ``⎿ result`` into the same entry, and going SUCCESS.

    Two runs isolate the two states deterministically (no timing): a started-only
    stream leaves the row RUNNING+marked+spinning; a started+completed stream
    leaves it SUCCESS with a static ``tool(args)`` + ``⎿ result`` body."""
    # In flight: marker present, state RUNNING, body renders the live indicator.
    live_app = TextualChatApp(transport=ScriptedTransport([_started("op-a")], end=False))
    async with live_app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(live_app, "tool_call_started")[0]
        assert entry.state is EntryState.RUNNING
        assert (entry.item.meta or {}).get(_RUNNING_SINCE_KEY) is not None
        pres = await ReynPresenter().present(entry, 80)
        assert pres.height == 2  # header + live indicator row
        assert any(frame in _render(pres.renderable) for frame in _SPINNER)

    # Completed: marker stripped, state SUCCESS, body is the coalesced result.
    done_app = TextualChatApp(
        transport=ScriptedTransport([_started("op-a"), _completed("op-a")], end=False)
    )
    async with done_app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(done_app, "tool_call_started")[0]
        assert entry.state is EntryState.SUCCESS
        assert (entry.item.meta or {}).get(_RUNNING_SINCE_KEY) is None
        pres = await ReynPresenter().present(entry, 80)
        body = _render(pres.renderable)
        assert "⎿" in body  # the folded-in result sub-line
        assert not any(frame in body for frame in _SPINNER)  # no lingering spinner


@pytest.mark.asyncio
async def test_app_coalesces_a_failure_and_tints_it() -> None:
    """Tier 2b: RUNNING → ERROR also settles in place — the started row's live
    marker is stripped, the failure folds into the SAME entry as a coral
    ``⎿ ✗ …`` sub-line, and its state goes ERROR."""
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-e"), _failed("op-e")], end=False)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        started = _entry_by_kind(app, "tool_call_started")[0]
        assert started.state is EntryState.ERROR
        assert (started.item.meta or {}).get(_RUNNING_SINCE_KEY) is None
        pres = await ReynPresenter().present(started, 80)
        from reyn.interfaces.repl.renderer import _CC_ERR, _CC_ERR_BG

        # #3367: the failure tint is the dark failure BLOCK, never the coral
        # foreground colour reused as a background (that painted the row's text
        # in its own background and made it illegible).
        assert pres.background == _CC_ERR_BG
        assert pres.background != _CC_ERR
        assert "⎿" in _render(pres.renderable)


# ── Gate 5: coalesce into one entry (owner request) ───────────────────────────

@pytest.mark.asyncio
async def test_correlated_pair_coalesces_into_exactly_one_entry() -> None:
    """Tier 2b: a correlated ``tool_call_started`` + its matching completion
    produce EXACTLY ONE model entry carrying BOTH the call and the ``⎿`` result —
    not two stacked rows.

    Non-vacuity: the model length is asserted to be exactly 1 for the pair (a
    regression to the old append-a-second-row behavior would make it 2)."""
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-1"), _completed("op-1")], end=False)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        flow = app.query_one(FlowView)
        # Exactly one entry — the started row carrying the folded-in result — not a
        # second stacked result row.
        assert [e.item.kind for e in flow.entries] == ["tool_call_started"]
        entry = flow.entries[0]
        assert (entry.item.meta or {}).get(_RESULT_KIND_KEY) == "tool_call_completed"
        body = _render((await ReynPresenter().present(entry, 80)).renderable)
        assert "grep" in body and "⎿" in body  # call header AND result, one block


@pytest.mark.asyncio
async def test_uncorrelated_result_still_appends_its_own_entry() -> None:
    """Tier 2b: a ``tool_call_completed`` with NO matching started entry (already
    settled / uncorrelated) still appends as its OWN entry — the coalesce path
    must not swallow an uncorrelated result (no regression for the plain-fallback
    turn sequence)."""
    app = TextualChatApp(transport=ScriptedTransport([_completed("orphan")], end=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        flow = app.query_one(FlowView)
        # The uncorrelated result appears as its own row (not swallowed).
        assert [e.item.kind for e in flow.entries] == ["tool_call_completed"]


# ── Gate 3: drives the real mechanism live, releases on settle (no leak) ───────

@pytest.mark.asyncio
async def test_running_body_animates_live_then_stops_on_settle() -> None:
    """Tier 2b: the app's per-entry ``animate_entry`` RE-PRESENTS the RUNNING body
    live while it is on screen, and ``settle`` (``stop_entry_animation``) halts it
    — the settled row is not re-presented again (no leaked timer).

    Drives the REAL mechanism (real FlowView animation timers under ``run_test``)
    with a real counting presenter: the RUNNING row's present-count keeps climbing
    while in flight, then FREEZES after completion. The freeze is the no-leak
    witness — a leaked ``animate_entry`` timer would keep the count climbing."""
    transport = QueueTransport()
    presenter = _CountingPresenter()  # held locally — assert on its PUBLIC count
    app = _fast_app(transport=transport, presenter=presenter)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(_started("op-live"))
        await pilot.pause()
        # #4044: a fixed-duration pilot.pause(0.4) here made the re-present COUNT
        # a function of the machine's timer/scheduler latency, not just
        # ANIMATION_FPS — CI under load only landed 2 ticks where 3+ ran on a
        # light machine, taking down an unrelated docs-only PR (#4043). Wait on
        # the condition instead of a fixed duration (CLAUDE.md testing policy,
        # Time) — tests/_async_wait.py's own wait_until, unbounded (#4275: no
        # local timeout literal — a bounded failure constant picked to make
        # this one call pass is the same axis as lowering the count threshold
        # to make it pass — both just relocate the same flake).
        from tests._async_wait import wait_until

        await wait_until(lambda: presenter.present_counts.get("op-live", 0) >= 3)
        live_count = presenter.present_counts.get("op-live", 0)
        assert live_count >= 3, (
            f"running body did not animate live: {live_count} presents"
        )

        # Complete it → settle stops the per-entry animation (coalesce in place).
        await transport.push(_completed("op-live"))
        await pilot.pause()
        await pilot.pause(0.1)  # let the one settle re-present land
        settled_count = presenter.present_counts.get("op-live", 0)
        # After settle the body must not be presented again — no leaked timer.
        await pilot.pause(0.4)
        frozen_count = presenter.present_counts.get("op-live", 0)
        assert frozen_count == settled_count, (
            f"body kept animating after settle (leaked timer): "
            f"{settled_count} → {frozen_count}"
        )
        entry = app.query_one(FlowView).entries[0]
        assert entry.state is EntryState.SUCCESS


# test_offscreen_running_tool_is_not_animated removed (#4304, part of #3880):
# per this test's own docstring, "animate_entry auto-pauses when the entry
# scrolls out of the viewport" is flowview's own track_visibility mechanism —
# reyn only registers the animate_entry callback, it does not implement the
# off-screen pause decision itself. If this failed, it would be flowview's
# viewport-gating bug, not reyn's.


# ── Gate 4: no regression ─────────────────────────────────────────────────────

def test_gutter_blink_and_body_animation_are_distinct_rates() -> None:
    """Tier 1: ① (gutter blink) and ② (body spinner) are DISTINCT positive
    animation rates — ① stays the always-on ``animation_fps`` gutter clock, ② adds
    a per-entry body cadence. Neither is zero, so both animate."""
    assert TextualChatApp.ANIMATION_FPS > 0  # ① gutter blink clock (unchanged)
    assert TextualChatApp.RUNNING_BODY_FPS > 0  # ② per-entry body animation


@pytest.mark.asyncio
async def test_plain_row_is_not_given_a_live_body() -> None:
    """Tier 2b: a plain (non-tool) row is untouched by ② — no RUNNING marker, no
    live body — so the plain-fallback turn sequence is unchanged."""
    app = TextualChatApp(
        transport=ScriptedTransport([OutboxMessage(kind="agent", text="hello")], end=False)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(app, "agent")[0]
        assert entry.state is EntryState.DEFAULT
        assert _RUNNING_SINCE_KEY not in (entry.item.meta or {})
