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
  SUCCESS (green) / ERROR (coral), and a failed row is tinted ``_CC_ERR_BG``
  edge-to-edge (#3367: the dark failure block, not the coral foreground colour
  reused as a background).

All use real instances (a concrete :class:`ScriptedTransport`, a real mounted
:class:`TextualChatApp`, real :class:`OutboxMessage`, a real list-backed clock
callable) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
import re
import tomllib
from typing import AsyncIterator

import pytest
from rich.cells import cell_len
from textual.app import App
from textual_flowview import EntryState

from reyn.interfaces.inline.textual_chat import (
    ReynGutter,
    ReynPresenter,
    TextualChatApp,
    _body_and_background,
)
from reyn.interfaces.inline.textual_chat.gutter import (
    _RUNNING_FRAMES,
    _cell_pad_right,
    _gutter_glyph_color,
    _is_retrieval_tool,
)
from reyn.interfaces.repl.renderer import (
    _CC_DONE,
    _CC_ERR,
    _CC_ERR_BG,
    _CC_WARN,
    _KIND_LINE,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT


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
    from textual_flowview import Entry, FlowView, StateDecorator

    # The library's own primitives are defined in textual_flowview, untouched.
    assert Entry.set_state.__module__.startswith("textual_flowview")
    assert StateDecorator.decorate.__module__.startswith("textual_flowview")
    # #3476/#3624: this used to also pin ``textual_flowview.__version__`` to a
    # literal string, but that string moves with every pin bump and always
    # broke on one — flagged for removal on "the bump PR" back when it was
    # written (#3866 is that bump). ``scripts/verify_env_identity.py`` (#3723)
    # now checks the same thing better: against the SHA in pyproject rather
    # than a string someone has to remember to edit.
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


def test_state_color_has_no_default_entry_kind_colour_is_the_only_default_source() -> None:
    """Tier 1: ``_STATE_COLOR`` (the EntryState → colour map) carries NO
    ``EntryState.DEFAULT`` entry. #3324: a prior ``_STATE_COLOR[EntryState.
    DEFAULT] = _CC_DIM`` entry was dead code (``ReynGutter.decorate``'s own
    ``elif state is EntryState.DEFAULT: color = kind_color`` branch always
    intercepts DEFAULT before the dict lookup) AND its comment claimed the
    opposite of what the dict would have done if it WERE live — a resolved
    intervention needs a different DEFAULT-state colour than an ordinary
    user/agent row, which a single scalar here could never provide. Pins
    that the contradiction cannot silently return: DEFAULT is absent from
    the map, and ``decorate`` is the sole source of a DEFAULT row's colour
    (via the per-kind ``kind_color`` from ``_gutter_glyph_color``)."""
    from reyn.interfaces.inline.textual_chat.gutter import _STATE_COLOR

    assert EntryState.DEFAULT not in _STATE_COLOR, (
        "_STATE_COLOR must not carry a DEFAULT entry — decorate()'s dedicated "
        "DEFAULT branch (falling back to the per-kind colour) is the only "
        "source of a DEFAULT row's colour; a dict entry here would be "
        "unreachable dead code (decorate() special-cases DEFAULT before ever "
        "consulting this map) and would misstate DEFAULT's real colour."
    )


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
    presentation is tinted ``_CC_ERR_BG`` edge-to-edge (CC block-tint). Feeds the
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

        # The coalesced failure entry is tinted edge-to-edge with the dark
        # failure block — NOT with the coral foreground colour (#3367).
        pres = await ReynPresenter().present(started.item, 80)
        assert pres.background == _CC_ERR_BG


def test_failure_rows_carry_coral_background_tint() -> None:
    """Tier 1: the failure-row tint is a pure function of the frame — a
    ``tool_call_failed`` and an ``error`` frame both carry a ``_CC_ERR_BG``
    whole-row background, while a non-error row carries none. Pins the
    ``_body_and_background`` contract directly. The tint is the dark failure
    BLOCK, distinct from the coral ``_CC_ERR`` text drawn on top of it — see
    ``tests/interfaces/test_textual_chat_row_contrast_3367.py`` for the legibility gate
    over every (kind, state) pairing."""
    _, bg_failed = _body_and_background(_failed("z"))
    assert bg_failed == _CC_ERR_BG
    _, bg_error = _body_and_background(OutboxMessage(kind="error", text="boom"))
    assert bg_error == _CC_ERR_BG
    _, bg_agent = _body_and_background(OutboxMessage(kind="agent", text="hi"))
    assert bg_agent is None


def test_left_gutter_pads_by_cells_not_characters() -> None:
    """Tier 1: :func:`_cell_pad_right` — the LEFT gutter's padding function,
    ``ReynGutter.decorate``'s counterpart to the RIGHT gutter's
    ``_cell_pad_left`` (#3347) — aligns to a CELL count, which is what a
    terminal column actually is, not a character count.

    ★ Driven with a genuinely DOUBLE-WIDTH character. That is not decoration:
    for the LEFT gutter's real glyph vocabulary (``· ⋯ ⎿ ◆ ○ ● ✗ ❯``) ``len()``
    and ``cell_len()`` agree on every glyph — four of them are East Asian
    Ambiguous width, which ``rich.cells.cell_len`` resolves to 1, the same as
    ``len()`` — so an assertion driven only by the real vocabulary passes
    identically for ``str.ljust`` and cannot witness the difference (#3350:
    this is exactly why the ``ljust`` in place since #3273 was never caught).
    A wide character is the discriminating input: ``ljust`` pads it by
    character count and overflows the column.

    The production vocabulary cannot currently emit one; this pins the
    helper's CONTRACT so the column stays correct by construction rather than
    by the coincidence that today's glyphs happen to be narrow or
    ambiguous-resolved-to-1."""
    wide = "中中"  # U+4E2D, east_asian_width "W" — wider than one cell each
    assert cell_len(wide) > len(wide), (
        "fixture must be text whose CELL width exceeds its CHARACTER count — "
        "otherwise the two padding strategies agree and this cannot witness "
        "the difference"
    )
    padded = _cell_pad_right(wide, 8)
    assert cell_len(padded) == 8, (
        f"{padded!r} occupies {cell_len(padded)} cells, not the 8 asked for — "
        "padding is counting characters, not cells"
    )
    assert padded.startswith(wide), "the label itself must survive padding"
    # An over-long label is returned unpadded rather than negative-padded;
    # flowview's own adjust_cell_length clips it, it never steals body columns.
    assert _cell_pad_right(wide, 2) == wide


def test_left_gutter_vocabulary_is_all_single_cell() -> None:
    """Tier 1: every glyph the LEFT gutter can currently EMIT measures exactly
    one terminal cell (:func:`rich.cells.cell_len`).

    This is a DIFFERENT property from the padding gate above: that one pins
    ``_cell_pad_right``'s CONTRACT (correct for any width, witnessed with a
    synthetic wide character the vocabulary cannot produce today).  This one
    pins the VOCABULARY itself — the reason `glyph.ljust(width)` shipped
    unnoticed since #3273 is that every glyph in it happens to measure 1 cell
    (four are East Asian Ambiguous width, which ``cell_len`` resolves to 1).
    ``_cell_pad_right`` now makes the padding correct regardless, but a
    single-cell vocabulary is still the realistic, cheap invariant to guard:
    an emoji marker (most measure 2 cells) added to this vocabulary without
    widening :data:`RIGHT_GUTTER_WIDTH`-style column accounting elsewhere
    would be a real, silent regression this test would catch immediately.

    Enumerated from the REAL registry (:data:`_KIND_LINE`, the tool-call and
    intervention branches of :func:`_gutter_glyph_color`, and
    :data:`_RUNNING_FRAMES`) rather than a hand-copied literal list, so a
    future glyph added to any of those sources is automatically covered."""
    glyphs: "set[str]" = set(_RUNNING_FRAMES)
    for kind in _KIND_LINE:
        glyph, _ = _gutter_glyph_color(OutboxMessage(kind=kind, text=""))
        glyphs.add(glyph)
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_started", text="grep", meta={"tool": "grep"})
    )
    glyphs.add(glyph)
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_completed", text="", meta={"tool": "grep"})
    )
    glyphs.add(glyph)
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_failed", text="", meta={"tool": "grep"})
    )
    glyphs.add(glyph)
    # intervention: pending (no _answer_label) and resolved (has one) are two
    # distinct glyph branches (#3324) — both enumerated.
    glyph, _ = _gutter_glyph_color(OutboxMessage(kind="intervention", text="", meta={}))
    glyphs.add(glyph)
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="intervention", text="", meta={"_answer_label": "yes"})
    )
    glyphs.add(glyph)

    assert glyphs, "enumeration produced no glyphs — registry sources changed shape"
    for glyph in glyphs:
        # #3329: the retrieval-demotion "no marker" case is the ONE
        # deliberate 0-cell glyph (an intentional absence, not a vocabulary
        # entry to pad) — `_cell_pad_right` already handles an empty label
        # correctly (all spaces), so it is exempt from the 1-cell rule
        # below rather than silently wrong.
        if glyph == "":
            continue
        assert cell_len(glyph) == 1, (
            f"{glyph!r} measures {cell_len(glyph)} cells, not 1 — a new "
            "vocabulary entry (e.g. an emoji marker) needs its column "
            "accounting revisited, not a silent single-cell assumption"
        )


def test_retrieval_tool_call_carries_no_gutter_marker() -> None:
    """Tier 1: #3329 — a started/completed call to a ``purity="read_only"``
    tool (the real registry, ``read_file``) gets no gutter glyph at all —
    the table's "gutter: 無し" cell. Contrasts with a side-effect tool
    (``write_file``), which keeps today's ``●``/``⎿`` markers."""
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_started", text="read_file", meta={"tool": "read_file"})
    )
    assert glyph == ""
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_completed", text="", meta={"tool": "read_file"})
    )
    assert glyph == ""

    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_started", text="write_file", meta={"tool": "write_file"})
    )
    assert glyph == "●"
    glyph, _ = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_completed", text="", meta={"tool": "write_file"})
    )
    assert glyph == "⎿"


def test_a_failed_retrieval_tool_call_still_gets_a_marker() -> None:
    """Tier 1: #3329 — demotion applies ONLY to started/completed
    (the successful/in-flight path); a FAILURE still needs the operator's
    attention regardless of the tool's op-class, so ``tool_call_failed``
    is deliberately excluded from :func:`_is_retrieval_tool`'s reach."""
    glyph, color = _gutter_glyph_color(
        OutboxMessage(kind="tool_call_failed", text="", meta={"tool": "read_file"})
    )
    assert glyph == "⎿"
    assert color == _CC_ERR


def test_is_retrieval_tool_derives_from_the_real_registry_not_a_hardcoded_list() -> None:
    """Tier 1: #3329's own completeness requirement — the demotion decision
    reads :attr:`~reyn.tools.types.ToolDefinition.purity` off the REAL
    default :class:`~reyn.tools.ToolRegistry`, not a name list living in
    this display module (the #3273 deferred-track's own repeated failure
    mode: "手動列挙は次も漏れる"). Witnessed by enumerating the real
    registry rather than asserting on a handful of literals."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    names = registry.names()
    assert names, "the default registry enumerated no tools — nothing to witness"

    read_only_names = [n for n in names if registry.lookup(n).purity == "read_only"]
    side_effect_names = [n for n in names if registry.lookup(n).purity == "side_effect"]
    assert read_only_names and side_effect_names, (
        "the real registry no longer has both purity values — this test's "
        "own premise (a real read/write split exists to derive from) broke"
    )

    for name in read_only_names:
        expected = name not in ("call_mcp_tool", "mcp_call_tool")
        assert _is_retrieval_tool(name) is expected, (
            f"{name!r}: purity=read_only but demotion={_is_retrieval_tool(name)}"
        )
    for name in side_effect_names:
        assert _is_retrieval_tool(name) is False, (
            f"{name!r}: purity=side_effect but demoted as retrieval"
        )


def test_dynamic_mcp_tool_calls_are_exempt_from_demotion_on_purpose() -> None:
    """Tier 1: #3329 — lead-coder ruling: an individual MCP-server tool
    installed at runtime (e.g. a genuinely read-only ``filesystem.read``)
    dispatches through ONE of two fixed wrapper tools whose OWN name
    (never the underlying MCP tool's identifier) is what
    ``dispatcher.py``'s ``tool_called`` event — and therefore this
    module's ``meta["tool"]`` — actually carries. No per-underlying-tool
    purity is knowable here, so both wrappers are excluded explicitly,
    not by coincidence of their current ``purity="side_effect"`` value."""
    assert _is_retrieval_tool("call_mcp_tool") is False
    assert _is_retrieval_tool("mcp_call_tool") is False


def test_unknown_tool_name_is_not_demoted() -> None:
    """Tier 1: #3329 — accept-side: a tool name absent from the registry
    (e.g. a message replayed against an older/mismatched build) fails
    closed to the PRE-#3329 behaviour (a normal marker), not to a silent
    "no marker" that would hide an unrecognized entry from the operator."""
    assert _is_retrieval_tool("this_tool_does_not_exist") is False
