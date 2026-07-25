"""Phase 2 TUI-rebuild gates (#3273): state-colour gutter + running blink + failure tint.

These pin the three architect-specified Phase-2 gates:

- **flowview-unmodified** (Tier 1): the running blink is app-side — reyn pins
  textual-flowview to a git commit, the installed library is unmodified (its
  ``Entry.set_state`` / ``StateDecorator`` are the library's own), and the blink
  glyph selection + timer live in reyn modules only.
- **set_interval neuter strip** (Tier 2b): neutering the blink timer leaves a
  static, still-correct gutter — proving the blink is ADDITIVE, not load-bearing.
  Paired with a positive check that the blink DOES change the gutter across
  frames, so the strip gate is not vacuous.
- **state transition** (Tier 2b): a tool-call row goes RUNNING (amber) →
  SUCCESS (green) / ERROR (coral), and a failed row is tinted ``_CC_ERR``
  edge-to-edge.

All use real instances (a concrete :class:`ScriptedTransport`, a real
:class:`FlowModel`, real :class:`OutboxMessage`) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.app import App
from textual_flowview import EntryState, FlowModel

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
    functions — while the blink mechanism (gutter frame selection + the timer)
    lives entirely in reyn modules. This is the 'blink is app-side' contract."""
    import textual_flowview
    from textual_flowview import Entry, StateDecorator

    # The library's own primitives are defined in textual_flowview, untouched.
    assert Entry.set_state.__module__.startswith("textual_flowview")
    assert StateDecorator.decorate.__module__.startswith("textual_flowview")
    assert textual_flowview.__version__ == "0.3.0.dev0"

    # The gutter frame selection is reyn's, not a flowview subclass override.
    assert ReynGutter.decorate.__module__ == "reyn.interfaces.inline.textual_chat"
    # ReynGutter is a plain reyn class (structural FlowDecorator), not a flowview
    # subclass — it does not inherit any flowview implementation.
    assert not any(
        base.__module__.startswith("textual_flowview") for base in ReynGutter.__mro__[1:]
    )
    # The blink timer is wired app-side: TextualChatApp is a reyn class built on
    # Textual's own App (its set_interval), not a flowview fork.
    assert TextualChatApp.__module__ == "reyn.interfaces.inline.textual_chat"
    assert issubclass(TextualChatApp, App)
    assert isinstance(TextualChatApp.BLINK_INTERVAL, (int, float))


# ── Gate 2: set_interval neuter strip (+ non-vacuous positive) ────────────────

def test_blink_changes_the_gutter_frame_across_ticks() -> None:
    """Tier 2b: a RUNNING entry's gutter glyph DIFFERS between two blink frames.

    The non-vacuity guard for the strip gate below — it proves there is a real
    blink to neuter. Uses a real FlowModel + a mutable frame counter the
    decorator reads, exactly as the app wires it."""
    model: FlowModel = FlowModel()
    entry = model.append(_started("op-blink"))
    entry.set_state(EntryState.RUNNING)

    frame = {"n": 0}
    gutter = ReynGutter(blink_frame=lambda: frame["n"])

    frame["n"] = 0
    g0 = gutter.decorate(entry, 2, 1).plain
    frame["n"] = 1
    g1 = gutter.decorate(entry, 2, 1).plain
    assert g0 != g1, f"blink did not change the gutter glyph: {g0!r} == {g1!r}"


@pytest.mark.asyncio
async def test_neutered_blink_leaves_a_working_gutter_and_input() -> None:
    """Tier 2b: neutering the blink timer to a no-op leaves the app fully working.

    A strip-falsify gate — a subclass overrides ``_advance_blink`` to a no-op (a
    real subclass, no mock), fires the timer fast, then confirms the app is still
    fully functional: the RUNNING entry is modeled with a valid amber gutter (no
    crash), AND a Composer submit still routes through the transport. The blink
    is additive; correctness does not depend on it."""
    from reyn.interfaces.inline.textual_chat import Composer

    class _StaticBlinkApp(TextualChatApp):
        BLINK_INTERVAL = 0.01  # fire fast so the strip is exercised within the test

        def _advance_blink(self) -> None:  # neutered: never advances the frame
            pass

    transport = ScriptedTransport([_started("op-static")], end=False)
    app = _StaticBlinkApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        running = _entry_by_kind(app, "tool_call_started")
        assert running, "running tool entry was not modeled"
        entry = running[0]
        # App still works: the entry is RUNNING with a valid amber gutter.
        assert entry.state is EntryState.RUNNING
        gutter = ReynGutter(blink_frame=lambda: 0)
        deco = gutter.decorate(entry, 2, 1)
        assert deco.style == _CC_WARN
        assert deco.plain.strip() != ""
        # And the app is still responsive despite the neutered blink: a submit
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
        gutter = ReynGutter(blink_frame=lambda: 0)
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
        gutter = ReynGutter(blink_frame=lambda: 0)
        assert gutter.decorate(entry, 2, 1).style == _CC_DONE


@pytest.mark.asyncio
async def test_running_to_error_turns_gutter_coral_and_tints_failure_row() -> None:
    """Tier 2b: RUNNING → ERROR — a failed tool call transitions the started
    entry to ERROR (gutter ``_CC_ERR`` coral) AND the failure row itself is
    tinted ``_CC_ERR`` edge-to-edge (CC block-tint). Feeds the correlated
    started+failed pair and inspects both the started entry's gutter and the
    failed entry's presentation background."""
    transport = ScriptedTransport([_started("op-err"), _failed("op-err")], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        started = _entry_by_kind(app, "tool_call_started")[0]
        assert started.state is EntryState.ERROR
        gutter = ReynGutter(blink_frame=lambda: 0)
        assert gutter.decorate(started, 2, 1).style == _CC_ERR

        # The failure row is tinted coral edge-to-edge.
        failed_item = _entry_by_kind(app, "tool_call_failed")[0].item
        pres = await ReynPresenter().present(failed_item, 80)
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
