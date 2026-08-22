"""Phase ④ gates (#3283): a per-entry ELAPSED-TIME right gutter.

The umbrella spec listed three CANDIDATES for the right gutter: elapsed time,
turn cost/tokens, and a state chip. #3283's Step-1 grounding (issue thread)
found cost/tokens are CUMULATIVE-ONLY in ``BudgetTracker`` (no per-turn/entry
source) and a state chip would duplicate the left gutter's existing
:class:`~textual_flowview.EntryState` encoding — both were dropped
(owner-adjudicated). The content set landed is **elapsed time only**, shown
only on entries that actually have it (tool-call rows), via flowview's
additive ``right_decorator``/``right_gutter_width`` params
(:class:`~reyn.interfaces.inline.textual_chat.gutter.ReynTimingGutter`). A
follow-up owner question ("does elapsed survive restore?") was answered
**NO, by decision**: a persisted ``ChatMessage`` carries no timing field at
all, and widening that persisted shape is out of scope for a TUI gutter
decoration — a restored row's right gutter renders BLANK, never a
reconstructed/fabricated value.

These pin:

- **content for an entry that has it** (Tier 1 + Tier 2b): a RUNNING tool row
  shows a LIVE elapsed label; a SETTLED tool row shows its captured FINAL
  elapsed — assert on ``decorate()``'s rendered output / the mounted
  FlowView's composed row text, never the source meta field alone.
- **negative control** (Tier 1 + Tier 2b): an entry with no timing data —
  a plain user/agent row, or ANY restored row (live-only by decision) —
  renders an EMPTY right-gutter cell. No placeholder, no ``"0s"``.
- **left gutter unchanged** (Tier 1 + Tier 2b): the #3273 state contract
  (``ReynGutter``) is untouched; RUNNING→SUCCESS/ERROR transitions still
  drive the same glyph/colour.
- **geometry** (Tier 2b, ★ real-TTY-witnessed #3311 pattern): bidirectional
  containment (``y >= 0`` AND ``y + h <= screen_height``) for the FlowView and
  Composer, plus a not-squashed floor, at 80x24 AND 100x60 — a defect earlier
  in this arc pushed widgets OFF-SCREEN while ``display``/``height > 0``
  stayed true, so neither of those is used here.
- **narrow terminals** (Tier 2b): flowview's own geometry floor (body width
  ``max(1, content_width - left_w - right_w)``) never hides the right
  gutter — it stays fixed-width and the BODY squashes instead. Pinned at an
  extreme width to confirm containment still holds and nothing crashes.

All use real instances (a real :class:`~textual_flowview.FlowModel`/``Entry``,
a real mounted :class:`TextualChatApp`, real :class:`OutboxMessage`, a real
list-backed clock) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

import pytest
from textual_flowview import Entry, EntryState, FlowModel, FlowView

from reyn.interfaces.inline.textual_chat import (
    ReynGutter,
    ReynPresenter,
    ReynTimingGutter,
    TextualChatApp,
)
from reyn.interfaces.inline.textual_chat._meta_keys import (
    ELAPSED_SECS_KEY,
    RUNNING_SINCE_KEY,
)
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.gutter import RIGHT_GUTTER_WIDTH
from reyn.interfaces.inline.textual_chat.restore import (
    RESTORED_META_KEY,
    project_restored_frames,
)
from reyn.interfaces.repl.renderer import _CC_DONE, _CC_WARN
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage

# #3879 M4-0: QueueTransport / _started / _WidthRecordingPresenter moved to
# tests/_support/textual_chat_test_helpers.py (byte-identical) — other test
# modules import them from here, which the Stage-1 migration gate cannot
# tolerate once this file moves. Aliased back to the original module-local
# names so everything below is unchanged.
from tests._support.textual_chat_test_helpers import (  # noqa: E402
    QueueTransport,
)
from tests._support.textual_chat_test_helpers import (
    WidthRecordingPresenter as _WidthRecordingPresenter,
)
from tests._support.textual_chat_test_helpers import (
    started as _started,
)


def _completed(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="",
        meta={"tool": tool, "op_id": op_id, "result": {"op": tool, "count": 3}},
    )


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
    completion and inspect the settle — with the stream staying open in between
    (mirrors ``test_textual_chat_phase2b_live_tool_3283.py``'s helper)."""

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


def _painted_lines(flow: "FlowView[OutboxMessage]") -> "list[str]":
    """Every non-blank PAINTED row of the FlowView, gutters included, read off
    ``Widget.render_line`` — Textual's public paint surface.

    This is the surface that answers "is the right gutter on screen?".
    ``get_selection`` deliberately is NOT: from textual-flowview 0.9.0 the
    selection is confined to the BODY columns (the gutter is decoration, like a
    scrollbar, so a yank never carries gutter glyphs), so reading a gutter label
    out of a selection reports an empty gutter for a perfectly painted one."""
    lines = [flow.render_line(y).text.rstrip() for y in range(flow.size.height)]
    return [ln for ln in lines if ln.strip()]


def _entry_by_kind(app: TextualChatApp, kind: str):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == kind]


class _WidthRecordingPresenter(ReynPresenter):
    """A real :class:`ReynPresenter` SUBCLASS (not a mock) that records the
    ``width`` FlowView hands ``present()`` for each entry — this is the BODY
    width (content width minus BOTH gutters), the one surface that actually
    exposes what the right gutter's column cost the conversation content.
    ``FlowView.region.width`` stays the FULL terminal width regardless of
    gutter configuration (gutter consumption is internal to flowview and not
    otherwise observable from outside it) — a co-vet finding on #3337, this
    class is the fix: it reads the real value off the real collaboration
    seam, not a private flowview attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.widths: "list[int]" = []

    async def present(self, entry: "Entry[OutboxMessage]", width: int):
        self.widths.append(width)
        return await super().present(entry, width)


# ── Gate 1: content for an entry that has it (Tier 1, pure decorate()) ────────


def test_running_tool_row_shows_live_elapsed_in_the_right_gutter() -> None:
    """Tier 1: a ``tool_call_started`` entry carrying the LIVE start marker
    renders a NON-EMPTY elapsed label — read off the clock, not a stashed
    field — so it advances as the clock advances (paired with the frozen-clock
    strip below for non-vacuity)."""
    model: "FlowModel[OutboxMessage]" = FlowModel()
    entry = model.append(
        OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={"tool": "grep", "op_id": "op-1", "args": {}, RUNNING_SINCE_KEY: 100.0},
        )
    )
    gutter = ReynTimingGutter(clock=lambda: 104.0)
    label = gutter.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    assert label == "4s", label


def test_live_elapsed_advances_with_the_clock_frozen_clock_is_static() -> None:
    """Tier 1: non-vacuity pair — the live label CHANGES as the clock advances,
    and a FROZEN clock leaves it static (so the positive read above is
    load-bearing, not a tautology)."""
    model: "FlowModel[OutboxMessage]" = FlowModel()
    entry = model.append(
        OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={"op_id": "op-1", RUNNING_SINCE_KEY: 0.0},
        )
    )
    now = [1.0]
    moving = ReynTimingGutter(clock=lambda: now[0])
    a = moving.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    now[0] = 9.0
    b = moving.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    assert a != b, f"live elapsed did not advance: {a!r} -> {b!r}"

    frozen = ReynTimingGutter(clock=lambda: 5.0)
    c = frozen.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    d = frozen.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    assert c == d, "frozen clock must not animate the right gutter"


def test_settled_tool_row_shows_its_captured_final_elapsed() -> None:
    """Tier 1: a SETTLED ``tool_call_started`` entry (no live marker, a
    captured :data:`ELAPSED_SECS_KEY`) renders that static value, unaffected
    by the clock — the app-computed final duration, not a re-derivation."""
    model: "FlowModel[OutboxMessage]" = FlowModel()
    entry = model.append(
        OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={"op_id": "op-1", ELAPSED_SECS_KEY: 12},
        )
    )
    gutter = ReynTimingGutter(clock=lambda: 9999.0)
    label = gutter.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    assert label == "12s", label


# ── Gate 2: negative control — no data, no display (Tier 1) ───────────────────


@pytest.mark.parametrize(
    "item",
    [
        OutboxMessage(kind="agent", text="hello"),
        OutboxMessage(kind="user", text="hi"),
        OutboxMessage(
            kind="tool_call_started", text="grep", meta={"op_id": "op-1"}
        ),  # settled-shaped but no captured elapsed (pre-instrumentation history)
        OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={"op_id": "op-1", RESTORED_META_KEY: True},
        ),  # a RESTORED tool row: no timing key at all (live-only by decision)
    ],
)
def test_entries_with_no_timing_data_render_an_empty_right_gutter(
    item: OutboxMessage,
) -> None:
    """Tier 1: the negative control — an entry with NEITHER
    :data:`RUNNING_SINCE_KEY` nor :data:`ELAPSED_SECS_KEY` renders NOTHING:
    no placeholder, no ``"0s"``. Covers a plain row, a plain tool row that
    never went through the live-elapsed instrumentation, and an explicitly
    RESTORED tool row (the live-vs-restore decision, pinned directly)."""
    model: "FlowModel[OutboxMessage]" = FlowModel()
    entry = model.append(item)
    gutter = ReynTimingGutter()
    label = gutter.decorate(entry, RIGHT_GUTTER_WIDTH, 1).plain.strip()
    assert label == "", f"expected an empty right gutter, got {label!r}"


def test_restore_projection_never_stamps_a_timing_key() -> None:
    """Tier 1: pins the live-vs-restore decision at the SOURCE, not just at
    the gutter's blank render — ``project_restored_frames`` never stamps
    :data:`RUNNING_SINCE_KEY` or :data:`ELAPSED_SECS_KEY` on a projected tool
    frame, for a correlated tool call+result fixture."""
    log = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(role="tool", content="3 hits", name="grep", tool_call_id="call_1"),
    ]
    frames = project_restored_frames(log)
    tool_frames = [f for f in frames if f.kind == "tool_call_started"]
    assert tool_frames, "fixture must project a tool_call_started frame"
    for frame in tool_frames:
        meta = frame.meta or {}
        assert RUNNING_SINCE_KEY not in meta
        assert ELAPSED_SECS_KEY not in meta


# ── App-level settle: the elapsed value is actually captured (Tier 2b) ────────


@pytest.mark.asyncio
async def test_app_stashes_final_elapsed_when_a_running_tool_settles() -> None:
    """Tier 2b: the mounted app captures the FINAL elapsed seconds into
    :data:`ELAPSED_SECS_KEY` at settle time (``_coalesce_tool_result``),
    stripping :data:`RUNNING_SINCE_KEY` — asserted on the entry's PUBLIC
    ``item.meta`` (the display-frame contract), not private app state."""
    now = [1000.0]
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: now[0])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(_started("op-a"))
        await pilot.pause()
        entry = _entry_by_kind(app, "tool_call_started")[0]
        assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None

        now[0] = 1006.0
        await transport.push(_completed("op-a"))
        await pilot.pause()
        await pilot.pause()

        entry = _entry_by_kind(app, "tool_call_started")[0]
        meta = entry.item.meta or {}
        assert RUNNING_SINCE_KEY not in meta
        assert meta.get(ELAPSED_SECS_KEY) == 6


@pytest.mark.asyncio
async def test_app_stashes_final_elapsed_for_an_orphaned_tool_too() -> None:
    """Tier 2b: an orphaned RUNNING tool (no completion frame ever arrives,
    force-settled at the turn boundary, #72) also gets a captured final
    elapsed — it ran for a real, observed duration before being swept."""
    now = [2000.0]
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-b")], end=False),
        clock=lambda: now[0],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        now[0] = 2003.0
        app._sweep_orphaned_running_tools()
        await pilot.pause()

        entry = _entry_by_kind(app, "tool_call_started")[0]
        meta = entry.item.meta or {}
        assert RUNNING_SINCE_KEY not in meta
        assert meta.get(ELAPSED_SECS_KEY) == 3
        assert entry.state is EntryState.CANCELLED


# ── Gate 1/2 end-to-end: real mounted FlowView, rendered row text ─────────────


@pytest.mark.asyncio
async def test_right_gutter_wired_end_to_end_settled_tool_vs_plain_row() -> None:
    """Tier 2b: through the REAL mounted FlowView (real ``right_decorator`` /
    ``right_gutter_width`` wiring), a SETTLED tool row's composed line ends in
    the elapsed label and a plain agent row's composed line does not — read off
    ``FlowView.render_line`` (Textual's public paint surface, which composes
    left gutter + body + right gutter), asserting on the RENDERED row text
    rather than the source meta field.

    Also pins the complement, since the two are easy to confuse: the same label
    is ABSENT from ``get_selection``. Since textual-flowview 0.9.0 a selection
    is confined to the body columns, so a yank carries the message text and no
    gutter glyphs — that is the contract, not a missing gutter."""
    now = [500.0]
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: now[0])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(OutboxMessage(kind="agent", text="hello there"))
        await transport.push(_started("op-x"))
        await pilot.pause()
        now[0] = 505.0
        await transport.push(_completed("op-x"))
        await pilot.pause()
        await pilot.pause()

        flow = app.query_one(FlowView)
        lines = _painted_lines(flow)

        # The settled tool row's own line carries its captured elapsed label
        # at the RIGHT-hand end (the right gutter) — positive content check.
        tool_lines = [ln for ln in lines if "grep" in ln]
        assert tool_lines, f"no rendered row found for the tool row: {lines!r}"
        assert any(ln.endswith("5s") for ln in tool_lines), (
            f"settled tool row's right gutter did not show the elapsed label: {tool_lines!r}"
        )
        # The plain agent row carries NO elapsed label anywhere on its line —
        # the negative control, read on the SAME rendered surface.
        agent_lines = [ln for ln in lines if "hello there" in ln]
        assert agent_lines, f"no rendered row found for the agent row: {lines!r}"
        assert not any(re.search(r"\d+[smh]$", ln) for ln in agent_lines), agent_lines

        # A "Complement" block asserting flow.get_selection() excludes the
        # gutter glyphs was removed here (#4304, part of #3880): per its own
        # comment, that's "flowview 0.9.0 confines selection to the body" —
        # flowview's own selection-scoping contract, not any reyn behavior.


# ── Gate 3: left gutter unchanged (#3273 state contract) ──────────────────────


def test_left_gutter_running_success_colours_unchanged() -> None:
    """Tier 1: the LEFT gutter's RUNNING (amber) / SUCCESS (green) colour
    contract — pinned identically to the existing #3273/#3283① coverage — is
    untouched by adding the right gutter. Reuses :class:`ReynGutter` directly."""
    model: "FlowModel[OutboxMessage]" = FlowModel()
    running = model.append(_started("op-1"))
    running.set_state(EntryState.RUNNING)
    done = model.append(_completed("op-2"))
    done.set_state(EntryState.SUCCESS)

    gutter = ReynGutter()
    assert gutter.decorate(running, 2, 1).style == _CC_WARN
    assert gutter.decorate(done, 2, 1).style == _CC_DONE


@pytest.mark.asyncio
async def test_left_gutter_state_transitions_unchanged_with_right_gutter_wired() -> None:
    """Tier 2b: through the mounted app (now wiring BOTH gutters), a tool row
    still transitions RUNNING → SUCCESS exactly as before ④ — no regression
    to the #3273 state contract from adding the right-gutter decorator."""
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-c"), _completed("op-c")], end=False)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(app, "tool_call_started")[0]
        assert entry.state is EntryState.SUCCESS


# ── Gate 3b: bounded gutter share — the right gutter cannot silently grow ─────
# to swallow the body. The earlier gates (containment, narrow-terminal squash)
# treat squashing the BODY as the expected absorber for a narrow terminal —
# which means, read alone, they impose NO UPPER BOUND on how much of an
# ordinary-width terminal the right gutter itself may claim. A co-vet finding
# on #3337: at ``RIGHT_GUTTER_WIDTH=40`` on an 80-column terminal (HALF the
# screen), every existing gate in this file still passed — the right gutter
# eats every row's width, but only tool rows ever populate it. This gate
# closes that hole, measured on the actual BODY width FlowView hands the
# presenter (``ReynPresenter.present(item, width)``) — the one surface that
# exposes gutter consumption; ``FlowView.region.width`` stays the full
# terminal width regardless (gutter cost is internal to flowview).


@pytest.mark.asyncio
async def test_right_gutter_leaves_the_body_at_least_half_the_terminal_width() -> None:
    """Tier 2b: ★ #3337 co-vet finding — the combined gutters (left state +
    right elapsed) must not claim more than HALF of an 80-column terminal's
    width; the BODY (the actual conversation content column) must keep AT
    LEAST half. Measured on the width :meth:`ReynPresenter.present` actually
    receives (:class:`_WidthRecordingPresenter`), not the widget's outer
    ``region`` (which stays the full terminal width regardless of how much
    of it flowview hands to gutters vs. body — the co-vet's measurement-plane
    correction).

    NON-VACUITY: the paired strip below drives the SAME scenario with
    ``RIGHT_GUTTER_WIDTH`` monkeypatched to 40 (half the 80-column screen)
    and asserts this same floor goes RED — proving the assertion is
    load-bearing against the actual defect shape the co-vet raised, not a
    tautology that would pass regardless of the configured width."""
    transport = QueueTransport()
    presenter = _WidthRecordingPresenter()
    app = TextualChatApp(transport=transport, presenter=presenter)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await transport.push(_started("op-share"))
        await pilot.pause()

        assert presenter.widths, "presenter never received a body width to record"
        body_width = presenter.widths[-1]
        assert body_width >= app.size.width // 2, (
            f"the right gutter left only {body_width} body columns out of "
            f"{app.size.width} — gutters are claiming more than half the "
            f"terminal width"
        )


@pytest.mark.asyncio
async def test_body_width_floor_is_load_bearing_against_an_oversized_right_gutter() -> None:
    """Tier 2b: the non-vacuity strip for the gate above — with
    ``RIGHT_GUTTER_WIDTH`` monkeypatched to 40 (half an 80-column terminal),
    the SAME body-width floor assertion FAILS. Confirms the positive gate
    above is not vacuously true regardless of configuration; this test
    itself is expected to fail its OWN inner assertion (caught and
    re-asserted as the outer expectation), so the suite stays green while
    still proving the floor is load-bearing."""
    import reyn.interfaces.inline.textual_chat.app as app_module

    original = app_module.RIGHT_GUTTER_WIDTH
    app_module.RIGHT_GUTTER_WIDTH = 40
    try:
        transport = QueueTransport()
        presenter = _WidthRecordingPresenter()
        app = TextualChatApp(transport=transport, presenter=presenter)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await transport.push(_started("op-oversized"))
            await pilot.pause()

            assert presenter.widths, "presenter never received a body width to record"
            body_width = presenter.widths[-1]
            floor_violated = body_width < app.size.width // 2
            assert floor_violated, (
                f"expected an oversized RIGHT_GUTTER_WIDTH=40 to violate the "
                f"body-width floor (got body_width={body_width}, "
                f"floor={app.size.width // 2}) — the floor assertion above is "
                f"not actually load-bearing against this defect shape"
            )
    finally:
        app_module.RIGHT_GUTTER_WIDTH = original


# ── Gate 4: geometry — bidirectional containment (★ #3311 pattern) ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(80, 24), (100, 60)])
async def test_right_gutter_does_not_push_widgets_off_screen(
    screen_size: "tuple[int, int]",
) -> None:
    """Tier 2b: ★ #3311-pattern regression guard — adding the RIGHT gutter
    (a second fixed-width column, wired via ``FlowView(right_gutter_width=…)``
    in ``app.compose``) must not change the widget geometry. With a RUNNING
    tool row active (right gutter actively painting a live label), the
    FlowView and Composer stay FULLY CONTAINED on-screen at TWO sizes — BOTH
    bounds (``y >= 0`` AND ``y + height <= screen_height``), plus a
    not-squashed floor on the FlowView — ``display``/``height > 0`` alone are
    proven insufficient earlier in this arc (#3311: a defective region can be
    non-zero height while pushed fully off-screen)."""
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-geo")], end=False)
    )
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await pilot.pause()

        flow = app.query_one(FlowView)
        composer = app.query_one(Composer)
        screen_height = app.size.height

        for name, widget in (("FlowView", flow), ("Composer", composer)):
            region = widget.region
            assert region.y >= 0, (
                f"{name}'s region is pushed OFF the top of the screen "
                f"(negative y); region={region!r}"
            )
            assert region.y + region.height <= screen_height, (
                f"{name}'s region extends past the bottom of the "
                f"{screen_height}-row screen; region={region!r}"
            )
        assert flow.region.height >= 3, (
            f"the FlowView is squashed to a hairline; region={flow.region!r}"
        )


# ── Gate 5: narrow terminals — the right gutter is never hidden, only the ─────
# body squashes (flowview's own floor: body_w = max(1, content_w - left_w -
# right_w)). Pinned so a future reader does not have to rediscover this by
# reading flowview source — this is the DECIDED behavior, not an accident.


@pytest.mark.asyncio
async def test_narrow_terminal_keeps_gutters_fixed_and_squashes_the_body() -> None:
    """Tier 2b: at an EXTREME narrow width, the app still mounts without
    crashing and the FlowView/Composer stay contained on-screen — the right
    gutter is NOT conditionally hidden below some width threshold; flowview's
    own body-width floor (``max(1, ...)``) absorbs the squeeze instead. This
    is the pinned, decided behavior for narrow terminals."""
    app = TextualChatApp(
        transport=ScriptedTransport([_started("op-narrow")], end=False)
    )
    async with app.run_test(size=(24, 20)) as pilot:
        await pilot.pause()
        await pilot.pause()

        flow = app.query_one(FlowView)
        composer = app.query_one(Composer)
        screen_height = app.size.height

        for widget in (flow, composer):
            region = widget.region
            assert region.y >= 0
            assert region.y + region.height <= screen_height

        # A trailing assert on the elapsed label's continued readability was
        # removed here (#4304, part of #3880): its own comment described it as
        # pinning "flowview's own body-width floor absorbs the squeeze" — while
        # reyn's own "never conditionally hide the gutter" decision is also
        # causally present, the assertion itself measured flowview's floor
        # formula's output, not reyn's decision directly. The mount/containment
        # checks above (reyn's own layout not crashing at extreme width) stay.
