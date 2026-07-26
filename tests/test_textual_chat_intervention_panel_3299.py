"""#3299 P1: intervention interaction moved into the grouped InterventionPanel.

Retargets the Phase-3.5 chip/match tests (#3273, ``test_textual_chat_phase35_3273.py``)
to the new panel-widget path — the ATOMIC display-swap + input-swap +
chip-retire this PR lands. The chip surface (``choice_chip_spans`` /
``_present_intervention_choice`` / ``on_flow_view_clicked`` /
``_match_choice_input`` / ``_surface_choice_hint`` / ``_CHOICE_*``) is retired
(grep-zero in ``src/``, see the PR body); a closed-set intervention is now
answered by SELECTING a :class:`~textual.widgets.RadioSet` option in the panel
(never a free-text label match — the #3290 type-or-click matching algorithm is
retired along with the Composer intervention-answer path it served, not
retargeted: the Composer is now exclusively for new turns), and a free-text
intervention by submitting the panel's own :class:`~textual.widgets.Input`.

Gates pinned here (per the architect's P1 scope ruling):

- **F1 permission-band reachability**: a real closed-set intervention frame
  populates the panel; selecting the SECOND RadioButton delivers exactly that
  option's ``choice_id`` through ``transport.answer_intervention_choice`` — the
  UNCHANGED transport funnel every answer path shares.
- **free-text reachability**: a free-text intervention's panel Input, once
  submitted, delivers through ``transport.answer_intervention_text``.
- **no-double-display / no-double-input (retire non-vacuity)**: the flow entry
  never renders chips (the presenter's chip-drawing symbols are gone — a
  direct grep-zero check — and the rendered flow-entry presentation carries
  only the prompt + a "respond in the panel below" hint, not the option
  labels); a Composer submit during a pending intervention is ALWAYS a new
  turn (``submit_user_text``), never an intervention answer.
- **focus-return**: Esc/Tab inside the panel returns focus to the Composer.
- **neutralization regression**: an LLM-derived choice label carrying raw
  terminal control sequences is still neutralized before it reaches the
  rendered flow-entry head (moved off the old chip-rendering path).

All use real instances (a concrete recording :class:`ClientTransport`, a real
mounted :class:`TextualChatApp`, real :class:`OutboxMessage`) — no mocks — per
the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, RadioButton, RadioSet, Static
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.intervention_choices import file_access_choices, generic_yn_choices
from reyn.runtime.outbox import OutboxMessage

_GUTTER_WIDTH = 2


class RecordingTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` that replays a fixed frame list
    and RECORDS which answer seam each user action reached.

    ``end=False`` keeps the stream open so the app stays mounted for
    inspection.
    """

    def __init__(
        self,
        messages: "list[OutboxMessage]",
        *,
        end: bool = False,
    ) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []
        self.answered_choice: list[str] = []
        self.answered_text: list[str] = []
        self.displayed: list[OutboxMessage] = []
        # (choice_id | text, intervention_id) pairs — records the id an answer
        # was targeted at (#3299 P2 R1 by-id delivery), ``None`` when the
        # caller left it head-targeted.
        self.answered_choice_ids: "list[str | None]" = []
        self.answered_text_ids: "list[str | None]" = []

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

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_text.append(text)
        self.answered_text_ids.append(intervention_id)
        return True

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_choice.append(choice_id)
        self.answered_choice_ids.append(intervention_id)
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _choice_intervention() -> OutboxMessage:
    """A closed-set (permission-confirm) intervention frame, shaped exactly like
    ``session._iv_meta`` builds it: structured ``prompt`` + ``choices`` (id /
    label / hotkey) plus the ``nodes`` render-model."""
    return OutboxMessage(
        kind="intervention",
        text="Allow write to /etc/hosts?\n  Yes / No / Always",
        meta={
            "intervention_id": "iv-1",
            "intervention_kind": "confirm",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
                {"id": "always", "label": "Always", "hotkey": "A"},
            ],
            "nodes": [
                {"component": "text", "text": "Allow write to /etc/hosts?"},
                {"component": "list", "items": ["Yes", "No", "Always"]},
            ],
        },
    )


def _free_text_intervention() -> OutboxMessage:
    """A free-text (no ``choices``) intervention frame."""
    return OutboxMessage(
        kind="intervention",
        text="What is the target directory?",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "ask_user",
            "prompt": "What is the target directory?",
        },
    )


def _second_choice_intervention() -> OutboxMessage:
    """A second, distinct closed-set intervention (different id) — used by the
    multi-pending tests (#3299 P2) to exercise TWO simultaneously-outstanding
    interventions, which ``outstanding_interventions`` supports by design."""
    return OutboxMessage(
        kind="intervention",
        text="Overwrite existing file?\n  Yes / No",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "confirm",
            "prompt": "Overwrite existing file?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
            "nodes": [
                {"component": "text", "text": "Overwrite existing file?"},
                {"component": "list", "items": ["Yes", "No"]},
            ],
        },
    )


def _iv_entry(app: TextualChatApp):
    entries = [
        e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
    ]
    assert len(entries) == 1, f"expected one intervention entry, got {len(entries)}"
    return entries[0]


def _iv_entries(app: TextualChatApp):
    """All intervention flow entries, keyed by their ``intervention_id`` meta."""
    return {
        e.item.meta.get("intervention_id"): e
        for e in app.query_one(FlowView).entries
        if e.item.kind == "intervention"
    }


@pytest.mark.asyncio
async def test_choice_intervention_panel_selection_delivers_correct_choice_id() -> None:
    """Tier 2b: F1 permission-band reachability — a closed-set intervention
    frame populates the panel's RadioSet; selecting the SECOND option delivers
    that option's ``choice_id`` ("no") through ``answer_intervention_choice``,
    and the flow entry resolves to SUCCESS. Non-vacuous: the SECOND option is
    chosen (a first-option shortcut would fail) and the exact id is asserted;
    no answer is delivered before the selection."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, "panel did not show for a pending intervention"
        radio = panel.query_one("#iv-panel-choices", RadioSet)
        assert radio.display is True
        assert radio.has_focus, "panel did not auto-focus the RadioSet"

        # No answer is delivered until the user acts.
        assert transport.answered_choice == []

        # #3299 P2 owner decision (A): the panel pre-highlights the FIRST
        # option on appear, so ONE "down" now reaches the SECOND option ("No")
        # — two would have been needed pre-P2 (index -1 → 0 → 1).
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["no"], (
            f"choice not delivered; got {transport.answered_choice}"
        )
        assert transport.answered_choice_ids == ["iv-1"], (
            "answer not delivered BY ID (#3299 P2 R1)"
        )
        # Resolved reflection: DEFAULT gutter (#3299 P2 §5 — not SUCCESS, an
        # answered intervention is neither an outcome nor a failure) + panel
        # collapsed + focus returned to the Composer.
        resolved = _iv_entry(app)
        assert resolved.state is EntryState.DEFAULT
        assert resolved.item.meta.get("_answer_label") == "No"
        assert panel.display is False, "panel did not collapse after resolving"
        assert app.query_one(Composer).has_focus, (
            "focus did not return to the Composer after resolving"
        )


@pytest.mark.asyncio
async def test_free_text_intervention_answered_via_panel_input() -> None:
    """Tier 2b: a free-text intervention (no choices) populates the panel's
    Input (auto-focused); submitting delivers the text through
    ``answer_intervention_text``. Regression retarget of the #3273 Composer
    free-text test — the answer path moved from the Composer to the panel."""
    transport = RecordingTransport([_free_text_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        text_input = panel.query_one("#iv-panel-input", Input)
        assert text_input.display is True
        assert text_input.has_focus, "panel did not auto-focus the Input"

        await pilot.press("o", "k")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_text == ["ok"]
    assert transport.answered_text_ids == ["iv-2"]
    assert transport.submitted == []
    assert transport.answered_choice == []


@pytest.mark.asyncio
async def test_composer_submit_during_pending_intervention_is_always_a_new_turn() -> None:
    """Tier 2b: no-double-input — the Composer no longer special-cases a
    pending intervention at all (#3299 P1: it no longer reads
    ``pending_intervention_head()``). A Composer submit while a closed-set
    intervention is pending is ALWAYS routed to ``submit_user_text`` — never
    ``answer_intervention_choice`` / ``answer_intervention_text`` — even when
    the text happens to name an option. This is the atomic-swap witness: if
    the retired Composer-side matching were still live (a half-swap), this
    would instead deliver a choice answer."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("y", "e", "s")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.submitted == ["yes"]
    assert transport.answered_choice == []
    assert transport.answered_text == []


def test_chip_drawing_symbols_are_retired() -> None:
    """Tier 1: retire grep-zero (non-vacuity witness) — the presenter module no
    longer defines the chip-drawing surface at all. This would fail RED if the
    chip path were still live alongside the panel (double-display)."""
    from reyn.interfaces.inline.textual_chat import presenter as presenter_module

    for name in (
        "choice_chip_spans",
        "_present_intervention_choice",
        "_choice_chip",
        "_CHOICE_INDENT",
        "_CHOICE_GAP",
        "_CHOICE_HINT",
    ):
        assert not hasattr(presenter_module, name), f"{name} still defined — chip path not retired"

    from reyn.interfaces.inline.textual_chat import app as app_module

    for name in ("_match_choice_input", "_surface_choice_hint", "_CHOICE_HINT_TEXT"):
        assert not hasattr(app_module, name), f"{name} still defined — match path not retired"

    assert not hasattr(app_module.TextualChatApp, "on_flow_view_clicked"), (
        "on_flow_view_clicked still defined — chip click handler not retired"
    )


@pytest.mark.asyncio
async def test_pending_intervention_flow_entry_has_no_chip_options_rendered() -> None:
    """Tier 2b: no-double-display — the flow entry for a pending intervention
    renders ONLY the prompt head + a dim hint pointing at the panel, never the
    option labels (which live exclusively in the panel's RadioSet now).
    Non-vacuous: the pre-#3299 chip presentation rendered every option label
    inline (``[ 1 · Yes ]`` etc.) — asserting none of those labels appear in
    the flow-entry rendering is exactly what a surviving chip branch would
    violate."""
    from rich.console import Console

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        presentation = await app._presenter.present(entry.item, 80)

    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "Allow write to /etc/hosts?" in rendered
    assert "respond in the panel below" in rendered
    # The old chip layout rendered every option as ``[ n · label ]`` inline —
    # asserting that shape is absent is exactly what a surviving chip branch
    # would violate.
    assert "[ 1 ·" not in rendered and "[ 2 ·" not in rendered, (
        "chip-shaped option markup leaked into the pending flow entry"
    )


@pytest.mark.asyncio
async def test_escape_and_tab_from_panel_return_focus_to_composer() -> None:
    """Tier 2b: focus-return — Esc/Tab inside the panel return focus to the
    Composer WITHOUT answering; the intervention stays pending (the panel
    stays open, mirroring the Phase-3 drawer's deterministic focus-flow)."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(InterventionPanel)
        assert panel.query_one("#iv-panel-choices", RadioSet).has_focus

        await pilot.press("escape")
        await pilot.pause()

        assert app.query_one(Composer).has_focus, "Esc did not return focus to the Composer"
        assert panel.display is True, "Esc must not answer/collapse the panel"
        assert transport.answered_choice == []
        assert transport.answered_text == []

        # Re-focus the panel to exercise Tab too.
        panel.query_one("#iv-panel-choices", RadioSet).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert app.query_one(Composer).has_focus, "Tab did not return focus to the Composer"
        assert panel.display is True
        assert transport.answered_choice == []


def test_choice_labels_are_neutralized_before_rendering() -> None:
    """Tier 2c: an LLM-derived choice LABEL carrying raw terminal control
    sequences must not leak into the flow entry's rendering — retargeted off
    the retired chip path onto :meth:`ReynPresenter._present_intervention_pending`,
    which renders the prompt head (labels themselves now live only in the
    panel's RadioSet, built by :meth:`InterventionPanel.show_choice` from the
    SAME raw ``meta["choices"]`` — Textual's own :class:`RadioButton` renders
    its label through Rich markup, not raw ANSI passthrough, so the injection
    surface pinned here is the flow-entry head, matching the original test's
    payload and assertions).

    NON-VACUITY (falsification): neutering ``presenter._neutralized_label`` to
    identity makes the raw ``\\x1b`` leak into the rendered head and flips this
    assertion RED — verified locally, so a silent removal of the
    neutralization is caught here."""
    from rich.console import Console

    from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter

    payload = "\x1b[31mDANGER\x1b]0;pwn\x07"
    msg = OutboxMessage(
        kind="intervention",
        text="Allow write?",
        meta={
            "intervention_id": "iv-x",
            "intervention_kind": "confirm",
            "prompt": payload,
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )

    presentation = ReynPresenter()._present_intervention_pending(msg, 80)
    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "\x1b" not in rendered, f"raw ESC leaked into the flow-entry head: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into the flow-entry head: {rendered!r}"
    assert "DANGER" in rendered


@pytest.mark.asyncio
async def test_bracket_decorated_option_labels_render_intact() -> None:
    """Tier 1: the panel's RadioButton must expose the FULL literal option
    label — the real-TTY-witnessed display bug where the FIRST character of
    every option label was dropped ("Yes" → "es", "No" → "o", "just this path
    always" → "ust this path always", "recursive under '...' always" →
    "ecursive under '...' always").

    Root cause: real choice labels are conventionally hotkey-bracket-decorated
    (``reyn.intervention_choices.generic_yn_choices`` / ``file_access_choices``
    — ``"[y]es"``, ``"[A]lways"``, ``"[n]o"``, ``"[N]ever"``,
    ``"[j]ust this path always"``, ``"[r]ecursive under '...' always"``).
    ``RadioButton``/``ToggleButton`` builds its label via
    ``Content.from_text(label)`` with Textual MARKUP PARSING ON by default for
    a plain ``str`` — it reads a leading ``[y]`` as an (unknown, unclosed)
    style tag and strips it from the rendered text, eating the bracket AND
    the enclosed hotkey letter.

    Asserts on the PUBLIC ``RadioButton.label`` (the widget's own exposed
    rendered label, not private state) for the REAL ``InterventionChoice``
    factories — the standard yes/no/always/never set AND the longer
    bracket-decorated labels (``file_access_choices``).

    NON-VACUITY (falsification): on the pre-fix code (``RadioButton(label)``
    with a bare ``str``) this assertion FAILS — every label above renders with
    its first character (and the ``[x]`` bracket) missing. Verified locally by
    reverting the ``Content(label)`` fix in ``InterventionPanel.show_choice``.
    """

    class _PanelHost(App):
        def compose(self) -> ComposeResult:
            yield InterventionPanel(id="panel")

    yn_choices = generic_yn_choices()
    long_choices = file_access_choices("/tmp/project")

    app = _PanelHost()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)

        panel.show_choice(
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey} for c in yn_choices
            ],
        )
        await pilot.pause()
        radio = panel.query_one("#iv-panel-choices", RadioSet)
        rendered_yn = [rb.label.plain for rb in radio.query(RadioButton)]
        assert rendered_yn == ["[y]es", "[A]lways", "[n]o", "[N]ever"], (
            f"bracket-decorated label(s) dropped a character; got {rendered_yn!r}"
        )

        panel.show_choice(
            prompt="Grant file access?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey} for c in long_choices
            ],
        )
        await pilot.pause()
        radio = panel.query_one("#iv-panel-choices", RadioSet)
        rendered_long = [rb.label.plain for rb in radio.query(RadioButton)]
        assert rendered_long == [
            "[y]es",
            "[j]ust this path always",
            "[r]ecursive under '/tmp/project' always",
            "[N]o",
        ], f"long/bracket-decorated label(s) dropped a character; got {rendered_long!r}"


# --- panel neutralize-guard witnesses (3 independently-witnessed surfaces) --
# The panel is a NEW rendering surface for LLM-derived text that only ever
# reached the flow entry before #3299 P1: the choice LABEL, the title/prompt,
# and the detail. Each of the three is neutralized at its own call site in
# ``InterventionPanel`` (``show_choice`` for the label, ``_set_head`` for
# prompt/detail) — a malicious LLM-controlled choice label / prompt / detail
# could otherwise drive the terminal (CSI color codes, OSC title-set) since
# Textual's own ``Content`` constructor does NOT strip ESC (0x1B) — only a
# narrow control-code set (BEL/BS/VT/FF/CR). Each test below is scoped to
# assert ONLY the ESC byte's absence (the byte Content's own stripping never
# removes), so each is a genuine, independent witness of ITS site's
# ``_neutralized_label`` call — reverting any ONE call (verified locally,
# see each docstring) flips ONLY that site's assertion RED, never the other
# two silently covering for it.
_ESC_OSC_PAYLOAD = "\x1b[31mRED\x1b]0;pwn\x07"


class _PanelOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield InterventionPanel(id="panel")


@pytest.mark.asyncio
async def test_panel_choice_label_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the panel's RadioButton LABEL surface is independently
    neutralized. A closed-set intervention choice carrying a raw ESC/OSC
    payload as its label (a malicious/compromised LLM-derived label — this is
    NOT hypothetical: ``meta["choices"]`` labels reach the panel RAW, copied
    verbatim by ``session._iv_meta``) must not leak the raw ESC byte into the
    mounted ``RadioButton.label`` Textual actually renders.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``_neutralized_label`` call around the label in
    ``InterventionPanel.show_choice`` (i.e. mounting
    ``RadioButton(Content(str(c.get("label", ""))))`` instead of
    ``RadioButton(Content(_neutralized_label(...)))``) makes this assertion
    FAIL — ``Content``'s own control-code stripping does not remove ESC
    (0x1B), only BEL/BS/VT/FF/CR, so the raw ESC survives all the way into
    the rendered label unless THIS site's neutralize call does the work.
    Reverting the title/detail neutralize (the other two sites) does NOT
    affect this assertion — the label survives its own site's guard alone."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.show_choice(
            prompt="Proceed?",
            detail=None,
            choices=[{"id": "x", "label": _ESC_OSC_PAYLOAD, "hotkey": "x"}],
        )
        await pilot.pause()
        radio = panel.query_one("#iv-panel-choices", RadioSet)
        (only_button,) = radio.query(RadioButton)
        rendered_label = only_button.label.plain
        assert "\x1b" not in rendered_label, (
            f"raw ESC leaked into the panel's RadioButton label: {rendered_label!r}"
        )
        assert "RED" in rendered_label


@pytest.mark.asyncio
async def test_panel_title_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the panel's TITLE/prompt surface is independently neutralized.
    An intervention prompt carrying a raw ESC/OSC payload must not leak into
    the mounted ``#iv-panel-title`` Static's rendered content.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``_neutralized_label`` call around ``prompt`` in
    ``InterventionPanel._set_head`` (passing ``Content(prompt)`` directly)
    makes this assertion FAIL, for the same reason as the label site —
    ``Content`` does not strip ESC on its own. Reverting the label or detail
    neutralize (the other two sites) does NOT affect this assertion."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.show_text(prompt=_ESC_OSC_PAYLOAD, detail=None)
        await pilot.pause()
        title = panel.query_one("#iv-panel-title", Static)
        rendered = title.content.plain
        assert "\x1b" not in rendered, (
            f"raw ESC leaked into the panel's title: {rendered!r}"
        )
        assert "RED" in rendered


@pytest.mark.asyncio
async def test_panel_detail_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the panel's DETAIL surface is independently neutralized. An
    intervention detail carrying a raw ESC/OSC payload must not leak into the
    mounted ``#iv-panel-detail`` Static's rendered content.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``_neutralized_label`` call around ``detail`` in
    ``InterventionPanel._set_head`` (passing ``Content(detail or "")``
    directly) makes this assertion FAIL, for the same reason as the label and
    title sites. Reverting the label or title neutralize (the other two
    sites) does NOT affect this assertion."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.show_text(prompt="Proceed?", detail=_ESC_OSC_PAYLOAD)
        await pilot.pause()
        detail = panel.query_one("#iv-panel-detail", Static)
        rendered = detail.content.plain
        assert "\x1b" not in rendered, (
            f"raw ESC leaked into the panel's detail: {rendered!r}"
        )
        assert "RED" in rendered


# --- #3299 P2: multi-pending by-id delivery + re-route ----------------------
# The architect's self-review finding (P1 merge): the panel was SINGLE-slot
# (a second pending intervention silently overwrote the first's entry handle)
# and delivery was HEAD-targeted (``answer_intervention_choice`` carries no
# id) — but ``outstanding_interventions`` legitimately holds MULTIPLE pending
# entries (e.g. restore's FIFO re-enqueue), so a stale head could receive an
# answer the user actually gave to a DIFFERENT, currently-displayed
# intervention. P2 fixes both: every pending intervention gets its OWN
# tracked flow entry (never overwritten), and an answer is delivered BY ID to
# whichever intervention the panel is showing.


@pytest.mark.asyncio
async def test_second_pending_intervention_does_not_overwrite_the_first() -> None:
    """Tier 2b: with TWO interventions pending, the panel keeps showing the
    FIRST (the architect's overwrite finding) — its flow entry is not
    orphaned, and the second gets its own placeholder entry too. Non-vacuous:
    pre-P2 the single ``_pending_iv_entry`` slot was overwritten by the
    second arrival (verified against the P1 source removed in this PR)."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        entries = _iv_entries(app)
        assert set(entries) == {"iv-1", "iv-2"}, (
            f"expected both pending interventions to get their own flow entry, got {set(entries)}"
        )
        # The panel still shows the FIRST (iv-1's prompt), not overwritten by
        # the second arrival.
        title = app.query_one(InterventionPanel).query_one("#iv-panel-title", Static)
        assert "hosts" in title.content.plain, (
            f"panel switched away from the first pending intervention; title={title.content.plain!r}"
        )


@pytest.mark.asyncio
async def test_multi_pending_answer_targets_the_displayed_intervention_by_id() -> None:
    """Tier 2b: ★non-vacuity witness for the mis-delivery fix — with TWO
    interventions pending, answering the one the panel DISPLAYS delivers to
    THAT intervention's id, and resolving it re-routes the panel to the
    other (FIFO), which then also delivers by its own id.

    The re-route does NOT pre-highlight (#3299 P2 co-vet safety fix,
    architect-agreed "safe side" call): a bare ``Enter`` right after the
    re-route must answer NOTHING — only after the user explicitly navigates
    (``Down``) does ``Enter`` deliver. Without this, a user's muscle-memory
    double-``Enter`` (answer the first, reflexively press Enter again) would
    silently confirm a DEFAULT option on the second intervention the user
    never actually looked at — an accidental permission grant, since an
    intervention can be a permission gate.

    NON-VACUITY (falsification):
    - stripping ``intervention_id=`` from
      ``TextualChatApp.on_intervention_panel_choice_selected`` /
      ``on_intervention_panel_text_submitted`` (reverting to
      head-targeted-only delivery) flips ``transport.answered_choice_ids`` to
      ``[None, None]`` instead of ``["iv-1", "iv-2"]``.
    - reverting ``_show_intervention``'s ``initial=False`` on re-route back
      to always pre-highlighting makes the bare-Enter-after-re-route
      assertion below FAIL (it would deliver "yes" instead of nothing) —
      verified locally."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # Answer the FIRST (displayed) intervention — pre-highlighted first
        # option ("Yes", #3299 P2 owner decision (A)), so a blind Enter
        # answers it.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"]
        assert transport.answered_choice_ids == ["iv-1"], (
            f"first answer not targeted at iv-1; got {transport.answered_choice_ids}"
        )
        entries = _iv_entries(app)
        assert entries["iv-1"].item.meta.get("_answer_label") == "Yes"
        assert entries["iv-2"].item.meta.get("_answer_label") is None, (
            "the second (still-pending) intervention's entry must not be touched"
        )

        # The panel re-routed to the SECOND (still pending) intervention —
        # never left blank/orphaned.
        panel = app.query_one(InterventionPanel)
        assert panel.display is True, "panel went blank instead of re-routing to the next pending intervention"
        title = panel.query_one("#iv-panel-title", Static)
        assert "Overwrite" in title.content.plain

        # ★co-vet safety fix: the re-route must NOT pre-highlight — a bare
        # Enter (the user's muscle-memory reflex right after answering the
        # first intervention) delivers NOTHING to the second, un-requested
        # intervention.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"], (
            "a bare Enter right after the re-route must not answer the "
            "un-requested second intervention (accidental-grant risk)"
        )
        entries = _iv_entries(app)
        assert entries["iv-2"].item.meta.get("_answer_label") is None, (
            "the second intervention must still be unanswered after a bare Enter"
        )
        assert panel.display is True, "panel must stay open — the bare Enter must not have resolved anything"

        # Only EXPLICIT navigation (Down highlights index 0, "Yes") then
        # Enter delivers.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes", "yes"]
        assert transport.answered_choice_ids == ["iv-1", "iv-2"], (
            f"second answer not targeted at iv-2; got {transport.answered_choice_ids}"
        )
        entries = _iv_entries(app)
        assert entries["iv-2"].item.meta.get("_answer_label") == "Yes"
        assert panel.display is False, "panel did not collapse once both resolved"
        assert app.query_one(Composer).has_focus


# --- #3299 P2: auto-focus (A) — blind Enter answers the first option --------


@pytest.mark.asyncio
async def test_blind_enter_answers_the_first_option_no_arrow_needed() -> None:
    """Tier 2b: owner decision (A), uniform across all closed-set
    interventions — on panel appear the FIRST option is pre-highlighted, so a
    bare ``Enter`` (no ``Down`` first) answers it immediately.

    NON-VACUITY (falsification): reverting the ``radio.action_next_button()``
    pre-highlight call in ``InterventionPanel.show_choice`` leaves
    ``RadioSet._selected`` unset (``-1``), so ``action_toggle_button`` (bound
    to Enter) is a no-op and NOTHING is delivered — this assertion would fail
    with an empty ``transport.answered_choice``, verified locally."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        radio = app.query_one(InterventionPanel).query_one("#iv-panel-choices", RadioSet)
        assert radio.has_focus

        await pilot.press("enter")  # no "down" first
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_choice == ["yes"], (
        f"a blind Enter did not answer the pre-highlighted FIRST option; got {transport.answered_choice}"
    )


# --- #3299 P2 §5: pending EntryState is DEFAULT, never RUNNING/SUCCESS/ERROR


@pytest.mark.asyncio
async def test_pending_intervention_entry_state_is_default_with_dim_awaiting_glyph() -> None:
    """Tier 2b: a PENDING intervention's flow entry stays ``EntryState.DEFAULT``
    (never ``RUNNING`` — would wrongly trip the #72 orphan-sweep + the ②
    live-spinner, an intervention is not a tool; never ``SUCCESS``/``ERROR`` —
    they imply an outcome, the #3296 don't-fabricate-a-classification lesson).
    The gutter distinguishes "awaiting" from an ordinary DEFAULT row with a
    dim kind-driven glyph instead of the state color.

    NON-VACUITY (falsification): reverting the ``kind == "intervention"``
    branch in ``gutter._gutter_glyph_color`` makes the pending glyph fall
    through to the ordinary (non-dim, "◆ needs you") intervention glyph
    regardless of pending/resolved — this assertion (dim colour while
    pending) would then fail, verified locally."""
    from textual_flowview import EntryState as _EntryState

    from reyn.interfaces.inline.textual_chat.gutter import ReynGutter
    from reyn.interfaces.repl.renderer import _CC_DIM

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        entry = _iv_entry(app)
        assert entry.state is _EntryState.DEFAULT
        assert entry.state not in (
            _EntryState.RUNNING,
            _EntryState.SUCCESS,
            _EntryState.ERROR,
        )
        gutter = ReynGutter()
        rendered = gutter.decorate(entry, width=2, height=1)
        assert rendered.style == _CC_DIM, (
            f"pending intervention gutter glyph is not dim; style={rendered.style!r}"
        )


# --- #3299 P2 §4: placeholder→resolved is the SAME entry, churn-zero --------


@pytest.mark.asyncio
async def test_resolve_updates_the_same_entry_no_new_entry_appended() -> None:
    """Tier 2b: ★non-vacuity witness for the churn-zero contract — resolving a
    pending intervention updates the SAME flow entry object in place; the SET
    of intervention entry objects is unchanged (identity-preserved) and the
    content becomes the Q→A record — never a second, additional entry.

    NON-VACUITY (falsification): if ``_resolve_pending_intervention`` APPENDED
    a new entry instead of ``entry.set_item(...)``-ing the tracked one (the
    owner's original "解凍後に別の flow entry が出る" churn complaint), the
    identity-set below would gain a SECOND, different entry object rather than
    staying exactly the one entry seen before resolving — this assertion
    would fail."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        before = {
            id(e) for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
        }
        entry_before = next(
            e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
        )

        await pilot.press("enter")  # blind Enter answers the pre-highlighted "Yes"
        await pilot.pause()
        await pilot.pause()

        after = {
            id(e) for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
        }
        assert after == before, (
            "resolving changed the SET of intervention flow-entry objects — "
            "churn regressed (a new entry was appended instead of updating in place)"
        )
        assert entry_before.item.meta.get("_answer_label") == "Yes", (
            "the SAME entry object was not updated in place with the resolved answer"
        )
