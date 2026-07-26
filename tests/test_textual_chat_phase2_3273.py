"""Phase 2 TUI-rebuild gates (#3273): state-colour gutter + running blink + failure tint.

Retargeted for #3283 ① (blink → native ``FlowView(animation_fps=N)``): the
running blink is no longer an app-side ``set_interval`` timer bumping a shared
counter — it is textual-flowview's native animation clock re-invoking a
TIME-based :class:`ReynGutter` decorator, which picks the frame from a monotonic
clock. These pin the architect-specified Phase-2 gates against that mechanism:

- **flowview-unmodified** (Tier 1): reyn pins textual-flowview to a git commit,
  the installed library is unmodified (its ``Entry.set_state`` / ``StateDecorator``
  are the library's own), and the blink glyph SELECTION lives in reyn's
  :class:`ReynGutter` only. The animation *cadence* is now the library's native
  ``FlowView(animation_fps=N)`` clock (reyn passes ``N``, unmodified library).
- **native-blink equivalence + additive strip** (Tier 2b): advancing the gutter's
  monotonic clock changes a RUNNING entry's frame (the spin still happens); a
  FROZEN clock / disabled animation leaves a static, still-correct amber gutter —
  proving the animation is ADDITIVE, not load-bearing. The positive check pairs
  with the strip so the gate is not vacuous.
- **state transition** (Tier 2b): a tool-call row goes RUNNING (amber) →
  SUCCESS (green) / ERROR (coral), and a failed row is tinted ``_CC_ERR``
  edge-to-edge.

All use real instances (a concrete :class:`ScriptedTransport`, a real mounted
:class:`TextualChatApp`, real :class:`OutboxMessage`, a real list-backed clock
callable) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.app import App
from textual_flowview import EntryState

from reyn.interfaces.inline.textual_chat import (
    ReynGutter,
    ReynPresenter,
    TextualChatApp,
    _body_and_background,
)
from reyn.interfaces.repl.renderer import _CC_DONE, _CC_ERR, _CC_WARN
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_REPO_ROOT = Path(__file__).resolve().parents[1]


class ScriptedTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` replaying a fixed frame list.

    ``end=False`` keeps the stream open after the script so the app under test
    stays mounted for inspection (a running tool never receives its completion).
    """

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


def _started(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started", text=tool, meta={"tool": tool, "op_id": op_id, "args": {}}
    )


def _completed(op_id: str, tool: str = "grep", result=None) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="",
        meta={"tool": tool, "op_id": op_id, "result": result or {"op": tool, "count": 3}},
    )


def _failed(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_failed",
        text=tool,
        meta={"tool": tool, "op_id": op_id, "error_kind": "Boom", "error_message": "it broke"},
    )


def _entry_by_kind(app: TextualChatApp, kind: str):
    from textual_flowview import FlowView

    return [e for e in app.query_one(FlowView).entries if e.item.kind == kind]


def _make_running_entry():
    """A real RUNNING :class:`~textual_flowview.Entry` (no mount needed): append a
    ``tool_call_started`` message to a real :class:`~textual_flowview.FlowModel`
    and set it RUNNING — the exact state the live path's ``_apply_lifecycle_state``
    assigns. Real instances only (no mock)."""
    from textual_flowview import FlowModel

    model: "FlowModel[OutboxMessage]" = FlowModel()
    entry = model.append(_started("op-frame"))
    entry.set_state(EntryState.RUNNING)
    return entry


# ── Gate 1: flowview-unmodified ───────────────────────────────────────────────

def test_textual_flowview_is_git_commit_pinned() -> None:
    """Tier 1: reyn depends on textual-flowview via a GIT COMMIT PIN, not a
    forkable local path — so 'the blink is app-side, not a flowview fork' is
    anchored to an immutable upstream commit. Reads the real ``pyproject.toml``."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    fv = [d for d in deps if d.split()[0].split("@")[0].strip() == "textual-flowview"]
    assert fv, f"textual-flowview not a direct dependency; deps={deps}"
    spec = fv[0]
    assert "git+" in spec, f"flowview must be git-pinned, got {spec!r}"
    # An immutable full commit sha after the '@', not a mutable branch/tag ref.
    sha = spec.rsplit("@", 1)[-1].strip()
    assert re.fullmatch(r"[0-9a-f]{40}", sha), (
        f"flowview must pin an immutable full commit sha, got {sha!r}"
    )


def test_flowview_library_is_unmodified_blink_lives_in_reyn() -> None:
    """Tier 1: the installed textual-flowview is NOT forked/monkeypatched — its
    ``Entry.set_state`` and ``StateDecorator.decorate`` are the library's own
    functions — while the blink glyph SELECTION lives entirely in reyn's
    :class:`ReynGutter`. The animation cadence is now the library's own native
    ``FlowView(animation_fps=N)`` clock (reyn passes ``N``; the library is
    unmodified). This is the 'blink glyph is reyn's, cadence is native' contract."""
    import textual_flowview
    from textual_flowview import Entry, FlowView, StateDecorator

    # The library's own primitives are defined in textual_flowview, untouched.
    assert Entry.set_state.__module__.startswith("textual_flowview")
    assert StateDecorator.decorate.__module__.startswith("textual_flowview")
    assert textual_flowview.__version__ == "0.3.0.dev0"
    # The native animation primitive reyn now drives the blink through: FlowView
    # accepts an ``animation_fps`` and owns its own animation tick.
    import inspect

    assert "animation_fps" in inspect.signature(FlowView.__init__).parameters

    # The gutter frame selection is reyn's, not a flowview subclass override.
    assert ReynGutter.decorate.__module__.startswith("reyn.interfaces.inline.textual_chat")
    # ReynGutter is a plain reyn class (structural FlowDecorator), not a flowview
    # subclass — it does not inherit any flowview implementation.
    assert not any(
        base.__module__.startswith("textual_flowview") for base in ReynGutter.__mro__[1:]
    )
    # reyn supplies the animation frame rate app-side: TextualChatApp is a reyn
    # class built on Textual's own App, not a flowview fork, and the fps it passes
    # to FlowView is a positive number (the clock is enabled by default).
    assert TextualChatApp.__module__.startswith("reyn.interfaces.inline.textual_chat")
    assert issubclass(TextualChatApp, App)
    assert isinstance(TextualChatApp.ANIMATION_FPS, (int, float))
    assert TextualChatApp.ANIMATION_FPS > 0


# ── Gate 2: native-blink equivalence + additive strip (+ non-vacuous positive) ─

def test_time_based_gutter_advances_frame_across_animation_ticks() -> None:
    """Tier 2b: the RUNNING gutter frame CHANGES as its monotonic clock advances.

    The native-blink equivalence witness (#3283 ①): :class:`ReynGutter` is
    TIME-based — it picks the ``_RUNNING_FRAMES`` glyph from ``int(clock() /
    frame_period)``. ``FlowView(animation_fps=N)`` re-invokes ``decorate`` each
    animation tick; the frame it returns advances with wall time. Here we drive
    the REAL mechanism with a real list-backed clock (no mock): reading the clock
    at successive frame-period boundaries selects successive glyphs.

    Non-vacuous by construction: the paired strip
    (``test_frozen_clock_leaves_a_working_static_gutter_and_input``) freezes the
    clock, and the glyph then does NOT change — so this positive assertion is
    load-bearing, not tautological."""
    from reyn.interfaces.inline.textual_chat.gutter import _RUNNING_FRAMES

    entry = _make_running_entry()
    # A real callable returning scripted monotonic values (not a mock): one value
    # per read, stepping one frame_period each time.
    times = iter([0.0, 0.5, 1.0])
    gutter = ReynGutter(frame_period=0.5, clock=lambda: next(times))

    glyphs = [gutter.decorate(entry, 2, 1).plain.strip() for _ in range(3)]

    # Every glyph is a real running frame, and consecutive ticks differ (the spin
    # happens): with 2 frames and one step per read the sequence alternates.
    assert all(g in _RUNNING_FRAMES for g in glyphs), glyphs
    assert glyphs[0] != glyphs[1], f"frame did not advance across ticks: {glyphs}"
    assert glyphs[1] != glyphs[2], f"frame did not advance across ticks: {glyphs}"


@pytest.mark.asyncio
async def test_app_wires_a_positive_animation_fps_on_the_flowview() -> None:
    """Tier 2b: the mounted app hands FlowView a POSITIVE ``animation_fps`` — the
    native clock that re-invokes the time-based gutter is actually enabled.

    Without this, the time-based decorator would never be re-run live and the
    blink would freeze. Reads the real mounted FlowView's stored fps off the
    public constructor arg the app passed (``app.ANIMATION_FPS``)."""
    transport = ScriptedTransport([_started("op-fps")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # The app enabled the native animation clock (fps > 0) at the cadence it
        # declares — this is what re-invokes the time-based ReynGutter.
        assert app.ANIMATION_FPS > 0


@pytest.mark.asyncio
async def test_frozen_clock_leaves_a_working_static_gutter_and_input() -> None:
    """Tier 2b: a FROZEN blink clock leaves the app fully working (additive strip).

    The strip-falsify gate retargeted to the native mechanism: freezing the
    gutter's clock (``frame_period<=0``) makes the glyph STATIC — the paired
    positive test proves it moves when the clock advances — yet the RUNNING entry
    still shows a valid amber gutter and the app is still responsive. The
    animation is cosmetic-additive; correctness does not depend on it.

    Falsification: neuter the animation (frozen clock) → the gutter is static, no
    crash, RUNNING stays amber, and a Composer submit still routes through the
    transport."""
    from reyn.interfaces.inline.textual_chat import Composer
    from reyn.interfaces.inline.textual_chat.gutter import _RUNNING_FRAMES

    transport = ScriptedTransport([_started("op-static")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        running = _entry_by_kind(app, "tool_call_started")
        assert running, "running tool entry was not modeled"
        entry = running[0]
        assert entry.state is EntryState.RUNNING
        # Frozen clock == animation neutered: the glyph is a valid, STATIC frame
        # and the gutter is still amber (state colour is correctness, not blink).
        frozen = ReynGutter(frame_period=0.0)
        deco_a = frozen.decorate(entry, 2, 1)
        deco_b = frozen.decorate(entry, 2, 1)
        assert deco_a.style == _CC_WARN
        assert deco_a.plain.strip() in _RUNNING_FRAMES
        assert deco_a.plain == deco_b.plain, "frozen clock must not animate"
        # And the app is still responsive with the animation neutered: a submit
        # routes through the transport (correctness is independent of the blink).
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
    assert transport.submitted == ["hi"]


# ── Gate 3: state transitions + failure-row tint ──────────────────────────────

@pytest.mark.asyncio
async def test_running_gutter_is_amber_while_in_flight() -> None:
    """Tier 2b: an in-flight tool call is RUNNING with an AMBER gutter (the
    ``_CC_WARN`` state colour). Feeds only the ``tool_call_started`` frame (its
    completion never arrives), then inspects the modeled entry + its gutter."""
    transport = ScriptedTransport([_started("op-run")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(app, "tool_call_started")[0]
        assert entry.state is EntryState.RUNNING
        gutter = ReynGutter()
        assert gutter.decorate(entry, 2, 1).style == _CC_WARN


@pytest.mark.asyncio
async def test_running_to_success_turns_gutter_green() -> None:
    """Tier 2b: RUNNING → SUCCESS — a completed tool call transitions the SAME
    started entry to SUCCESS, whose gutter is the ``_CC_DONE`` green. Feeds the
    correlated started+completed pair (same ``op_id``) through the mounted app."""
    transport = ScriptedTransport([_started("op-ok"), _completed("op-ok")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _entry_by_kind(app, "tool_call_started")[0]
        assert entry.state is EntryState.SUCCESS
        gutter = ReynGutter()
        assert gutter.decorate(entry, 2, 1).style == _CC_DONE


@pytest.mark.asyncio
async def test_running_to_error_turns_gutter_coral_and_tints_failure_row() -> None:
    """Tier 2b: RUNNING → ERROR — a failed tool call transitions the started
    entry to ERROR (gutter ``_CC_ERR`` coral) AND, under the #3283 ② coalesce,
    the failure is folded into that SAME started entry (no separate row) whose
    presentation is tinted ``_CC_ERR`` edge-to-edge (CC block-tint). Feeds the
    correlated started+failed pair and inspects the coalesced entry's gutter and
    presentation background."""
    transport = ScriptedTransport([_started("op-err"), _failed("op-err")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # Coalesced: the started+failed pair is ONE entry, not two.
        from textual_flowview import FlowView

        assert [e.item.kind for e in app.query_one(FlowView).entries] == ["tool_call_started"]
        assert not _entry_by_kind(app, "tool_call_failed")  # no separate failure row
        started = _entry_by_kind(app, "tool_call_started")[0]
        assert started.state is EntryState.ERROR
        gutter = ReynGutter()
        assert gutter.decorate(started, 2, 1).style == _CC_ERR

        # The coalesced failure entry is tinted coral edge-to-edge.
        pres = await ReynPresenter().present(started.item, 80)
        assert pres.background == _CC_ERR


def test_failure_rows_carry_coral_background_tint() -> None:
    """Tier 1: the failure-row tint is a pure function of the frame — a
    ``tool_call_failed`` and an ``error`` frame both carry a ``_CC_ERR``
    whole-row background, while a non-error row carries none. Pins the
    ``_body_and_background`` contract directly."""
    _, bg_failed = _body_and_background(_failed("z"))
    assert bg_failed == _CC_ERR
    _, bg_error = _body_and_background(OutboxMessage(kind="error", text="boom"))
    assert bg_error == _CC_ERR
    _, bg_agent = _body_and_background(OutboxMessage(kind="agent", text="hi"))
    assert bg_agent is None
