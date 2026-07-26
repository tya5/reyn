"""``InterventionPanel`` — the grouped panel widget for intervention answers.

#3299 P1 moves intervention interaction OUT of the FlowView (which was
append-only history rendering the interaction as in-flow clickable chips) and
INTO a bordered panel widget sitting between the flow and the input row. This
is an ATOMIC swap (display + input + chip-retire together — see the app/
presenter module docstrings for the coupling rationale): the panel is now the
ONLY place a pending intervention is answered.

The panel shows a title (the intervention prompt) + optional detail, and one
of two native Textual forms:

- **closed-set** (the frame carries ``meta["choices"]``) — a
  :class:`~textual.widgets.RadioSet` of the options, native ``↑``/``↓`` +
  ``Enter`` selection.
- **free-text** (no choices) — a plain :class:`~textual.widgets.Input`.

Selecting an option / submitting text posts a message
(:class:`InterventionPanel.ChoiceSelected` / :class:`InterventionPanel.TextSubmitted`)
that the app relays through the UNCHANGED transport funnel
(``answer_intervention_choice`` / ``answer_intervention_text`` —
``InterventionHandler.deliver_answer_to`` under the hood, the SAME funnel
every answer path shares). This widget never touches the transport itself —
that seam stays in exactly one place (the app), matching every other send
path in this package.

Focus lifecycle (mirrors the Phase-3 drawer's deterministic focus-flow): a
pending intervention auto-focuses the panel's form (blocking — answer now);
``Esc``/``Tab`` return focus to the Composer WITHOUT answering (an escape
hatch — the intervention stays pending, the panel stays open); a resolved
answer collapses the panel and returns focus to the Composer. ``Esc``/``Tab``
are declared as widget-level ``BINDINGS`` (not raw ``_on_key`` interception)
so Textual's own focused-widget-outward binding-chain resolution finds them
on this ancestor before falling through to ``Screen``'s default
``tab``→``focus_next`` (neither ``RadioSet`` nor ``Input`` bind ``escape`` or
``tab`` themselves, so nothing upstream swallows the key first).

This module is part of the TTY-only ``textual_chat`` package (imported lazily
via :mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never
reach an always-loaded module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Input, RadioButton, RadioSet, Static

from .presenter import _neutralized_label

if TYPE_CHECKING:
    pass


class InterventionPanel(Vertical):
    """Bordered panel between the FlowView and the input row, surfacing the
    ONE pending intervention as a focusable form. Collapsed (``display=False``)
    when nothing is pending — the default state until :meth:`show_choice` /
    :meth:`show_text` is called."""

    DEFAULT_CSS = """
    InterventionPanel {
        height: auto;
        border: round $warning;
        padding: 0 1;
        margin-top: 1;
    }
    InterventionPanel #iv-panel-title {
        text-style: bold;
        color: $warning;
    }
    InterventionPanel #iv-panel-detail {
        color: $text-muted;
    }
    InterventionPanel RadioSet {
        border: none;
        height: auto;
        padding: 0;
    }
    """

    #: Declared here (not raw ``_on_key``) so Textual's binding-chain
    #: resolution — which walks from the FOCUSED widget (the RadioSet/Input
    #: inside this panel) outward through its ancestors — finds this binding
    #: on the panel before it ever reaches ``Screen``'s own default
    #: ``tab``→``focus_next`` (see the module docstring).
    BINDINGS = [
        Binding("escape", "dismiss_panel", "Back to composer", show=False),
        Binding("tab", "dismiss_panel", "Back to composer", show=False),
    ]

    class ChoiceSelected(Message):
        """A closed-set option was picked — ``choice_id`` is the authoritative
        delivery key for ``transport.answer_intervention_choice``; ``label`` is
        carried alongside for the resolved flow-entry record (basic in P1 — the
        placeholder→resolved in-place churn-zero contract is P2)."""

        def __init__(self, choice_id: str, label: str) -> None:
            self.choice_id = choice_id
            self.label = label
            super().__init__()

    class TextSubmitted(Message):
        """A free-text answer was submitted in the panel's Input."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class Dismissed(Message):
        """``Esc``/``Tab`` pressed inside the panel — the app returns focus to
        the Composer. The intervention itself is NOT answered: it stays
        pending and the panel stays open (the escape hatch is focus-only)."""

    def compose(self):
        yield Static("", id="iv-panel-title")
        yield Static("", id="iv-panel-detail")
        yield RadioSet(id="iv-panel-choices")
        yield Input(id="iv-panel-input", placeholder="Type your answer…")

    def on_mount(self) -> None:
        self.display = False
        self._choice_ids: "list[str]" = []
        self._choice_labels: "list[str]" = []
        self.query_one("#iv-panel-choices", RadioSet).display = False
        self.query_one("#iv-panel-input", Input).display = False

    def _set_head(self, prompt: str, detail: "str | None") -> None:
        # ``_neutralized_label`` (the SAME terminal-neutralization boundary the
        # flow entry's prompt head uses, presenter.py's ``_intervention_head``)
        # strips control/ESC sequences from this LLM-derived text before it
        # reaches the widget — the panel is a NEW rendering surface for
        # ``prompt``/``detail`` (they previously only ever reached the flow
        # entry), so it must apply the same boundary.
        #
        # Then ``Content(...)`` (literal), never a bare ``str`` —
        # ``Static.update`` markup-parses a ``str`` by default too, so a
        # prompt/detail containing a literal ``[...]`` (e.g. a bracketed path)
        # would suffer the SAME character-eating bug the option labels had
        # (see :meth:`show_choice`'s ``Content(label)`` fix).
        self.query_one("#iv-panel-title", Static).update(
            Content(_neutralized_label(prompt))
        )
        detail_widget = self.query_one("#iv-panel-detail", Static)
        detail_widget.update(Content(_neutralized_label(detail or "")))
        detail_widget.display = bool(detail)

    def show_choice(
        self, *, prompt: str, detail: "str | None", choices: "list[dict]"
    ) -> None:
        """Populate + show the panel for a closed-set intervention, auto-
        focusing the :class:`RadioSet` (native ``↑``/``↓``/``Enter``
        selection) — a pending intervention blocks the turn, so it is
        answer-now."""
        self._set_head(prompt, detail)
        radio = self.query_one("#iv-panel-choices", RadioSet)
        for child in list(radio.children):
            child.remove()
        self._choice_ids = [str(c.get("id", "")) for c in choices]
        # Neutralized at THIS boundary (the SAME terminal neutralizer the
        # retired chip path applied to a label before drawing it, and the flow
        # entry still applies to its own head) — ``meta["choices"]`` labels
        # reach here RAW (``session._iv_meta`` copies ``choice.label``
        # verbatim). ``_choice_labels`` is also what ``ChoiceSelected`` carries
        # for the resolved flow-entry record, so neutralizing here covers both
        # this widget's own RadioButton AND that downstream record.
        self._choice_labels = [
            _neutralized_label(str(c.get("label", ""))) for c in choices
        ]
        for label in self._choice_labels:
            # ``Content(label)`` (the LITERAL constructor), never a bare
            # ``str`` — ``RadioButton``/``ToggleButton`` internally builds its
            # label via ``Content.from_text(label)`` with markup parsing ON by
            # default for a plain ``str``. Real choice labels are
            # conventionally hotkey-bracket-decorated (``"[y]es"``,
            # ``"[j]ust this path always"``, ``"[r]ecursive under …"`` — see
            # ``reyn.intervention_choices``), and Textual's markup parser reads
            # a leading ``[y]`` as an (unknown, unclosed) STYLE TAG — which is
            # stripped from the rendered text, eating the bracket AND the
            # hotkey letter (``"[y]es"`` → ``"es"``, ``"[j]ust…"`` → ``"ust…"``
            # — the #3299 first-character-dropped display bug). ``Content`` is
            # one of ``RadioButton``'s accepted input types and is returned
            # as-is (no markup parsing), so the full literal label always
            # renders intact.
            radio.mount(RadioButton(Content(label)))
        # #3299 P2 — owner decision (A), uniform: pre-highlight the FIRST
        # option so a blind ``Enter`` answers it immediately (no extra arrow
        # keypress first). ``RadioSet`` only auto-selects index 0 in its OWN
        # ``_on_mount`` (fired once, when this widget mounted with ZERO
        # children — the buttons above are mounted dynamically, later, so that
        # native behavior never fires for them); its OWN highlight-advance
        # action (``action_next_button``, bound to the Down key) reproduces
        # that native "select the first enabled button" behavior — but only
        # from an UNSET anchor (``RadioSet._selected is None``): a SECOND
        # ``show_choice`` call (the P2 multi-pending re-route showing the next
        # queued intervention) reuses this same widget instance, so its
        # highlight index from the PREVIOUS intervention survives the
        # children-swap above and would advance past index 0 instead of
        # landing on it. Resetting the highlight first makes every
        # ``show_choice`` call behave identically regardless of history.
        # This only moves the HIGHLIGHT (``RadioSet.pressed_index`` stays -1,
        # nothing is answered yet) — the user's own ``Enter``/``Space`` still
        # does the actual toggle-and-deliver.
        if self._choice_ids:
            radio._selected = None
            radio.action_next_button()
        radio.display = True
        self.query_one("#iv-panel-input", Input).display = False
        self.display = True
        self.call_after_refresh(radio.focus)

    def show_text(self, *, prompt: str, detail: "str | None") -> None:
        """Populate + show the panel for a free-text intervention, auto-
        focusing the :class:`Input`."""
        self._set_head(prompt, detail)
        self.query_one("#iv-panel-choices", RadioSet).display = False
        text_input = self.query_one("#iv-panel-input", Input)
        text_input.value = ""
        text_input.display = True
        self.display = True
        self.call_after_refresh(text_input.focus)

    def hide(self) -> None:
        """Collapse the panel (the intervention resolved)."""
        self.display = False
        self._choice_ids = []
        self._choice_labels = []

    def on_radio_set_changed(self, event: "RadioSet.Changed") -> None:
        index = event.radio_set.pressed_index
        if 0 <= index < len(self._choice_ids):
            event.stop()
            self.post_message(
                self.ChoiceSelected(self._choice_ids[index], self._choice_labels[index])
            )

    def on_input_submitted(self, event: "Input.Submitted") -> None:
        text = event.value.strip()
        if text:
            event.stop()
            self.post_message(self.TextSubmitted(text))

    def action_dismiss_panel(self) -> None:
        self.post_message(self.Dismissed())


__all__ = ["InterventionPanel"]
