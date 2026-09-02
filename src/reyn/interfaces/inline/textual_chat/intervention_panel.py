"""``InterventionPanel`` — the tab-ified grouped panel widget for intervention
answers.

#3299 P1 moved intervention interaction OUT of the FlowView and INTO a
bordered panel widget between the flow and the input row. P1/P2 gave the
panel a SINGLE form that either showed the head-of-queue intervention (P1) or
re-routed in place to the next queued one on resolve (P2, FIFO). #3308 (P5)
replaces that single-form re-route with a :class:`~textual.widgets.TabbedContent`:
every PENDING intervention gets its own :class:`~textual.widgets.TabPane`
(title + optional detail + its own :class:`~textual.widgets.RadioSet` /
:class:`~textual.widgets.Input`), added the moment its frame arrives
(:meth:`add_pending`) and never swapped out from under the user.

This is a STRUCTURAL fix for an accident the P2 re-route could still produce:
resolving the displayed intervention re-populated the SAME form in place, so
a user's muscle-memory second ``Enter`` right after answering could land on
an unread SECOND intervention's default option — an accidental permission
grant, since an intervention can be a permission gate (P2's ``pre_highlight``
flag suppressed the immediate symptom, but the re-route itself remained).
With tabs, answering a tab never moves the ACTIVE tab (Textual's own
``TabbedContent.add_pane`` only auto-activates a newly added pane when the
content was previously EMPTY — verified against the installed Textual
8.2.8, see :meth:`add_pending`'s docstring), so a second ``Enter`` after
resolving lands on the SAME (now-inert, ✓-marked) tab and delivers nothing.

**Invariant** (the one that must never regress): the ACTIVE tab moves ONLY
on an explicit user navigation (``Left``/``Right`` or a tab click) or on the
panel's hidden→shown transition (the first pending intervention while the
panel was idle). A new arrival while another intervention is already showing
is added as an inert background tab — never stealing the active selection.

**Answered tabs are never removed** — they stay, labelled with a ``✓``
prefix, their form ``disabled``, until the LAST pending intervention resolves
(:meth:`collapse_all`, called by the app once ``_pending_ivs`` is empty). The
Q→A record itself lives in the flow entry (churn-zero, #3299 P2 §4), so
nothing is lost when the panel eventually collapses.

**Keymap** (co-vet-corrected, #3308 issue comment — the original "no
conflict" claim was wrong): ``RadioSet`` already binds ``left``/``right`` as
aliases for ``up``/``down`` (Textual 8.2.8, ``down,right → next_button`` /
``up,left → previous_button``), so a naive ancestor binding on this panel for
``Left``/``Right`` would never fire while focus is inside a focused
``RadioSet`` — the focused widget's OWN binding wins in Textual's normal
focused-widget-outward walk. The fix is Textual's PRIORITY bindings
(``Binding(..., priority=True)``): ``App._check_bindings`` runs a priority
pass FIRST, checked from the outermost ancestor down to the focused widget
(``textual/app.py`` ~L3966-3988, ~L4136; ``textual/screen.py`` ~L407-455),
and only ``priority=True`` bindings participate in that pass — so a
priority binding on THIS panel is matched before the walk ever reaches the
focused ``RadioSet``'s own (non-priority) binding for the same key. Declared
here (not on ``RadioSet``/``TabbedContent`` themselves, which stay untouched)
as ``action_prev_tab``/``action_next_tab``. ``RadioSet`` option navigation is
now ``Up``/``Down`` only (its own ``Left``/``Right`` aliases are shadowed by
this priority binding whenever focus is inside this panel — a deliberate,
uniform loss of a redundant alias, not a functional regression, since
``Up``/``Down`` already do the same thing).

``Esc`` (from P1/P2; ``Tab``'s equivalent binding REMOVED, #3365): returns
focus to the Composer WITHOUT answering. Declared as a widget-level
(non-priority) ``BINDINGS`` entry so Textual's ordinary focused-widget-outward
resolution finds it on this ancestor before ``Screen``'s default handling.
#3365 (architect ruling): ``Tab`` is forward-only everywhere in the app — its
"back to composer" binding here was redundant with ``Esc``, and having BOTH
keys mean "back" in some places while ``Tab`` alone means "forward" elsewhere
(the MenuBar/composer-idle case) was exactly the "same key, opposite meaning"
inconsistency #3365 was filed to fix. Removing it was gated on
``test_textual_chat_esc_sufficiency_3365.py`` machine-verifying ``Esc`` alone
already reaches the Composer from every focus state this panel can hold
(including its ``Input`` — the specific future-regression risk that gate
exists to catch). #3327 gave this escape hatch a way BACK: the Composer's own
``↑`` (see
:class:`~reyn.interfaces.inline.textual_chat.chrome.Composer`'s ``_on_key``)
focuses this panel (:meth:`focus_pending`) FIRST, ahead of the sent-queue,
whenever :meth:`has_pending` is true — before #3327, ``Esc`` here was a
ONE-WAY trip for a keyboard-only user (no binding anywhere retargeted focus
onto this panel, and the documented ``/answer`` fallback was itself queued
behind the very intervention it targeted, a structural deadlock — see
``app.py``'s module docstring and :meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp._submit`).

Selecting an option / submitting text posts a message
(:class:`InterventionPanel.ChoiceSelected` / :class:`InterventionPanel.TextSubmitted`),
each carrying the ``key`` identifying WHICH pane/intervention it came from
(the same key :class:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp`
tracks in ``_pending_ivs``) — the app relays it through the UNCHANGED
transport funnel (``answer_intervention_choice`` / ``answer_intervention_text``,
targeted by id). This widget never touches the transport itself.

This module is part of the TTY-only ``textual_chat`` package (imported lazily
via :mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never
reach an always-loaded module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import (
    Input,
    RadioButton,
    RadioSet,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from reyn.interfaces import palette

from .presenter import _neutralized_label

if TYPE_CHECKING:
    from textual.widget import Widget

#: Tab-bar labels are kept compact — the full (neutralized) prompt is still
#: the pane BODY's title Static, this is only the tab strip's short caption.
#: A long LLM-authored prompt would otherwise blow out the tab bar.
_TAB_LABEL_MAX = 28


def _tab_label(prompt: str, *, answered: bool) -> Content:
    """The tab-bar caption for one intervention — ``Content(...)`` (the
    LITERAL constructor, never a bare ``str``, #3299's bracket-eating markup
    bug — see :meth:`InterventionPanel.add_pending`'s neutralization note),
    ✓-prefixed once answered (#3308 §4)."""
    head = prompt
    if len(head) > _TAB_LABEL_MAX:
        head = head[: _TAB_LABEL_MAX - 1] + "…"
    return Content(("✓ " if answered else "") + head)


class InterventionPanel(Vertical):
    """Bordered panel between the FlowView and the input row. One TabPane per
    PENDING intervention (#3308); collapsed (``display=False``) only while
    NOTHING is pending — the default state until the first :meth:`add_pending`."""

    #: ★ #3311 real-TTY regression: this CSS deliberately does NOT set a rule
    #: for ``Tabs`` (the internal tab-caption bar ``TabbedContent`` composes).
    #: An earlier revision of this file added ``InterventionPanel Tabs {
    #: height: auto; }`` alongside the ``TabbedContent`` rule below, by
    #: (wrong) analogy — but ``textual.widgets.Tabs`` ships its OWN sensible
    #: FIXED ``height: 2`` default (verified against the installed Textual
    #: 8.2.8: ``Tabs.DEFAULT_CSS``), and overriding a widget that is NOT
    #: designed for auto-sizing to ``height: auto`` resolved to a hugely
    #: inflated value (~30 rows on an 80x24 screen, tui-coder's real-TTY
    #: witness on #3311) — ballooning the whole panel (also ``height: auto``)
    #: and pushing the FlowView/Composer off-screen. Every widget-STATE
    #: assertion in the test suite stayed green throughout (the panel WAS
    #: displayed, WAS focused — just enormous), which is why
    #: ``test_pending_intervention_panel_does_not_swallow_the_screen`` /
    #: ``test_tabs_bar_height_is_the_native_fixed_two_rows`` assert on
    #: ``Widget.region`` (actual computed screen geometry) directly, not
    #: widget state. Only ``TabbedContent`` itself (which DOES default to
    #: ``height: auto`` — ``TabbedContent.DEFAULT_CSS`` — so overriding it is
    #: a no-op override, kept for documentation clarity) needs a rule here.
    DEFAULT_CSS = palette.css("""
    InterventionPanel {
        height: auto;
        border: round @attention@;
        padding: 0 1;
        margin-top: 1;
    }
    InterventionPanel TabbedContent {
        height: auto;
    }
    InterventionPanel .iv-pane-title {
        text-style: bold;
        color: @attention@;
    }
    InterventionPanel .iv-pane-detail {
        color: @quiet@;
    }
    InterventionPanel RadioSet {
        border: none;
        height: auto;
        padding: 0;
    }
    """)

    #: ``Esc``/``Tab`` stay ordinary (non-priority) bindings — the escape
    #: hatch, unchanged from P1/P2. ``Left``/``Right`` are PRIORITY bindings
    #: (see the module docstring's keymap section for why plain ancestor
    #: bindings cannot work here): they switch the active tab even while
    #: focus is inside a pane's ``RadioSet``/``Input``.
    BINDINGS = [
        # #3365: Tab's own "back to composer" binding was removed — Esc alone
        # now owns "back" everywhere (architect ruling: Tab is forward-only).
        # Safe only because test_textual_chat_esc_sufficiency_3365.py
        # machine-verifies Esc reaches the Composer from every focus state
        # this panel can hold, INCLUDING its Input — see that file's module
        # docstring for the #3327-shaped risk this gate specifically guards.
        Binding("escape", "dismiss_panel", "Back to composer", show=False),
        Binding(
            "left", "prev_tab", "Previous intervention", show=False, priority=True
        ),
        Binding(
            "right", "next_tab", "Next intervention", show=False, priority=True
        ),
    ]

    class ChoiceSelected(Message):
        """A closed-set option was picked in the tab identified by ``key`` —
        ``choice_id`` is the authoritative delivery key for
        ``transport.answer_intervention_choice``; ``label`` is carried
        alongside for the resolved flow-entry record."""

        def __init__(self, key: object, choice_id: str, label: str) -> None:
            self.key = key
            self.choice_id = choice_id
            self.label = label
            super().__init__()

    class TextSubmitted(Message):
        """A free-text answer was submitted in the tab identified by ``key``."""

        def __init__(self, key: object, text: str) -> None:
            self.key = key
            self.text = text
            super().__init__()

    class Dismissed(Message):
        """``Esc``/``Tab`` pressed inside the panel — the app returns focus to
        the Composer. No intervention is answered: every pending tab stays
        exactly as it was (the escape hatch is focus-only)."""

    def compose(self) -> ComposeResult:
        yield TabbedContent(id="iv-tabs")

    def on_mount(self) -> None:
        self.display = False
        self._pane_ids: "dict[object, str]" = {}
        self._key_by_pane: "dict[str, object]" = {}
        self._choice_ids: "dict[str, list[str]]" = {}
        self._choice_labels: "dict[str, list[str]]" = {}
        # #4751: per-pane hotkey list, index-aligned with ``_choice_ids``/
        # ``_choice_labels`` above (SAME zip source, ``choices`` in
        # :meth:`add_pending` — one iteration, not a second lookup that
        # could drift out of index-alignment with the other two).
        self._choice_hotkeys: "dict[str, list[str]]" = {}
        self._prompts: "dict[str, str]" = {}
        self._answered: "set[str]" = set()
        self._next_pane_num = 0

    def add_pending(
        self,
        key: object,
        *,
        prompt: str,
        detail: "str | None",
        choices: "list[dict] | None",
    ) -> None:
        """Add ONE new tab for a newly-arrived pending intervention (#3308).

        Textual's own ``TabbedContent.add_pane`` semantics give the "only
        auto-focus while idle" invariant for free (verified directly against
        the installed Textual 8.2.8: ``add_pane`` activates the added pane
        ONLY when the ``TabbedContent`` was previously empty — ``active ==
        ""`` — never on a SUBSEQUENT add): the panel's hidden→shown
        transition activates the new tab; an arrival while another
        intervention is already showing is added WITHOUT moving the active
        selection — never stealing focus/selection from a tab the user is
        already looking at, the F1-class accident this PR closes structurally."""
        pane_id = f"iv-pane-{self._next_pane_num}"
        self._next_pane_num += 1
        self._pane_ids[key] = pane_id
        self._key_by_pane[pane_id] = key
        # THREE INDEPENDENT neutralization call sites (#3308 AC7) — the tab
        # label, the pane title, and the pane detail are three separate
        # LLM-derived-text rendering surfaces (``meta["prompt"]``/``["detail"]``
        # reach here RAW, copied verbatim by ``session._iv_meta``); each is
        # neutralized at ITS OWN call site (not shared/reused) so a strip of
        # any ONE site flips only that site's witness test RED, never the
        # other two silently covering for it (the SAME per-site discipline
        # P1's ``_set_head`` used for prompt vs. detail).
        tab_label_text = _neutralized_label(prompt)
        # Stashed for :meth:`mark_answered`'s ✓-relabel — reusing this
        # ALREADY-neutralized value there is not a fourth witness site, just
        # reuse of a value this same call already produced.
        self._prompts[pane_id] = tab_label_text
        title_text = _neutralized_label(prompt)
        body: "list[Widget]" = [Static(Content(title_text), classes="iv-pane-title")]
        if detail:
            detail_text = _neutralized_label(detail)
            body.append(Static(Content(detail_text), classes="iv-pane-detail"))
        if choices:
            self._choice_ids[pane_id] = [str(c.get("id", "")) for c in choices]
            # ``Content(label)`` (LITERAL), never a bare ``str`` — see
            # ``RadioButton``/``ToggleButton``'s markup-parse-on-bare-str
            # behavior and the #3299 bracket-eating bug this avoids.
            self._choice_labels[pane_id] = [
                _neutralized_label(str(c.get("label", ""))) for c in choices
            ]
            # #4751: ``InterventionChoice.hotkey`` already rides the wire
            # (every producer stamps it — ``session.py``/
            # ``intervention_handler.py``/``status.py``/``app.py``'s own
            # dict-normalization for the hydrated-head case) but this
            # panel never READ it before now. An empty string for a choice
            # with no hotkey (never emitted today, but not assumed) simply
            # never matches any keypress in :meth:`on_key` below.
            self._choice_hotkeys[pane_id] = [
                str(c.get("hotkey", "")) for c in choices
            ]
            radio = RadioSet(
                *(RadioButton(Content(label)) for label in self._choice_labels[pane_id])
            )
            body.append(radio)
        else:
            body.append(Input(placeholder="Type your answer…"))
        tabs = self.query_one(TabbedContent)
        tabs.add_pane(TabPane(_tab_label(tab_label_text, answered=False), *body, id=pane_id))
        self.display = True

    def mark_answered(self, key: object, answer_label: str) -> None:
        """Mark ``key``'s tab ✓-answered and inert its form (#3308 §4 — a
        second, muscle-memory ``Enter``/``Space`` on this tab must be a
        visible no-op, not rely solely on the server's typed reject). The tab
        STAYS mounted — never removed until :meth:`collapse_all`."""
        pane_id = self._pane_ids.get(key)
        if pane_id is None:
            return
        self._answered.add(pane_id)
        tabs = self.query_one(TabbedContent)
        try:
            pane = tabs.get_pane(pane_id)
        except Exception:
            return
        for control in pane.query(RadioSet):
            control.disabled = True
        for control in pane.query(Input):
            control.disabled = True
        tab = tabs.get_tab(pane_id)
        tab.label = _tab_label(self._prompts.get(pane_id, ""), answered=True)
        # Disabling the focused control moves Textual's focus elsewhere
        # (verified empirically: a focused widget going ``disabled`` auto-
        # blurs to the app's next focusable widget) — if that would carry
        # focus OUT of this panel entirely while OTHER interventions remain
        # pending, the Left/Right priority binding (#3308 AC8) would no
        # longer be in the focused widget's ancestor chain at all. Re-anchor
        # focus on the panel's own ``Tabs`` bar instead (still inside this
        # panel, and itself Left/Right-navigable) — but ONLY when the
        # answered pane was the ACTIVE one (an answer delivered from a
        # background tab, #3308 AC3's out-of-order case, never had focus to
        # begin with and must not steal it).
        if tabs.active == pane_id:
            tabs.query_one(Tabs).focus()

    def collapse_all(self) -> None:
        """Collapse the whole panel — called by the app ONLY once every
        pending intervention has resolved (#3308 §4): the Q→A record already
        lives in the flow entry (churn-zero, #3299 P2 §4), so nothing is lost
        when the tab strip itself goes away."""
        self.query_one(TabbedContent).clear_panes()
        self.display = False
        self._pane_ids.clear()
        self._key_by_pane.clear()
        self._choice_ids.clear()
        self._choice_labels.clear()
        self._choice_hotkeys.clear()
        self._prompts.clear()
        self._answered.clear()

    def on_key(self, event: events.Key) -> None:
        """#4751 (owner ruling, issuecomment on the issue thread — "表示どお
        りに配線する"): a bracketed choice hotkey (``[y]``/``[j]``/``[r]``/
        ``[N]`` etc.) moves the RadioSet's selection to that choice —
        SELECTS only, never confirms (arrow+Enter stays the sole way to
        answer, unchanged). Case-sensitive, matching ``InterventionChoice``/
        ``match_choice``'s own contract (``user_intervention.py``) — the
        REPL/stdin path already treats ``n``/``N`` as two different
        choices in the same set (one-shot deny vs. persistent deny), so a
        case-insensitive match here would let this panel answer a question
        the hotkey scheme was never asked.

        Scoped to whichever tab is ACTIVE and matched against ONLY that
        tab's own choices — never a global keymap. This is an ordinary
        (non-``priority``) handler: Textual's normal focused-widget-outward
        bubble already reaches this ancestor before the App (the same
        mechanism the module docstring's keymap section documents for
        ``Esc``), and a bare letter has no existing ``RadioSet``/``Input``
        binding to out-race — unlike ``Left``/``Right`` above, nothing here
        needs ``priority=True`` (architect's own #4751 analysis: a
        priority binding would cross the focus boundary and reopen the
        ``r``/``/rewind`` double-use architect flagged as a real risk;
        this handler never does).

        Deny side (#4751 acceptance): an unmatched character is left alone
        — not consumed, not acted on. It does not reach the Composer
        either (the Composer only receives key events while IT holds
        focus, a mutually exclusive state from this panel holding it —
        see the module docstring's own analysis of #3299's pinned test),
        so the net effect for an unmatched key while a pane is focused is
        unchanged from before this PR: silently dropped by ``RadioSet``'s
        own handling, exactly as measured on the issue thread."""
        pane_id = self.query_one(TabbedContent).active
        if not pane_id or pane_id in self._answered:
            return
        character = event.character
        if character is None:
            return
        hotkeys = self._choice_hotkeys.get(pane_id, [])
        if character not in hotkeys:
            return
        index = hotkeys.index(character)
        try:
            pane = self.query_one(TabbedContent).get_pane(pane_id)
        except Exception:
            return
        radios = list(pane.query(RadioSet))
        if not radios:
            return
        event.stop()
        radios[0]._selected = index

    def on_radio_set_changed(self, event: "RadioSet.Changed") -> None:
        pane = next(
            (a for a in event.radio_set.ancestors if isinstance(a, TabPane)), None
        )
        if pane is None or pane.id is None:
            return
        pane_id = pane.id
        index = event.radio_set.pressed_index
        ids = self._choice_ids.get(pane_id, [])
        labels = self._choice_labels.get(pane_id, [])
        if 0 <= index < len(ids):
            event.stop()
            key = self._key_by_pane.get(pane_id)
            self.post_message(self.ChoiceSelected(key, ids[index], labels[index]))

    def on_input_submitted(self, event: "Input.Submitted") -> None:
        pane = next(
            (a for a in event.input.ancestors if isinstance(a, TabPane)), None
        )
        if pane is None or pane.id is None:
            return
        text = event.value.strip()
        if text:
            event.stop()
            key = self._key_by_pane.get(pane.id)
            self.post_message(self.TextSubmitted(key, text))

    def on_tabbed_content_tab_activated(
        self, event: "TabbedContent.TabActivated"
    ) -> None:
        """Fires uniformly on the panel's hidden→shown transition AND on
        every explicit tab switch (verified against the installed Textual
        8.2.8 — ``add_pane``'s auto-activation of the first pane posts this
        SAME message) — focuses the newly-active pane's form.

        Pre-highlighting the FIRST option (owner decision (A), #3308 §5:
        uniform on first show AND every tab switch) needs NO extra code
        here: unlike P1/P2's single re-routed form (one ``RadioSet`` reused
        across different interventions, needing an explicit reset+re-highlight
        dance on each swap), #3308 gives every pending intervention its OWN
        ``RadioSet`` instance, created once in :meth:`add_pending` — Textual's
        OWN ``RadioSet._on_mount`` unconditionally pre-highlights index 0 the
        moment that instance is constructed (verified against the installed
        Textual 8.2.8), and that highlight persists whether or not the tab is
        currently active. Re-deriving it here would RACE against that native
        mount-time highlight (observed empirically: double-calling
        ``action_next_button()`` after mount could advance PAST index 0).

        An already-ANSWERED tab (its form ``disabled`` by :meth:`mark_answered`)
        is left alone — nothing to focus, its selection is the historical
        answer."""
        pane = event.pane
        if pane.id is None or pane.id in self._answered:
            return
        radios = list(pane.query(RadioSet))
        if radios:
            self.call_after_refresh(radios[0].focus)
            return
        inputs = list(pane.query(Input))
        if inputs:
            self.call_after_refresh(inputs[0].focus)

    def has_pending(self) -> bool:
        """Whether at least one tab is still UNANSWERED (#3327) — the
        Composer's ``↑`` keyboard-reachability route reads this to decide
        whether ``↑`` should re-focus this panel (ahead of the sent-queue)
        instead of moving the cursor / falling through to the sent-queue's
        own ``↑`` handling. Mirrors :attr:`display` (the panel is shown iff
        something is pending, and :meth:`collapse_all` clears both together)
        but reads the public tab-tracking surface directly rather than a
        CSS-facing attribute."""
        return bool(self._pane_ids)

    def focus_pending(self) -> None:
        """Re-focus the ACTIVE tab's form — the keyboard route BACK into
        this panel (#3327) after ``Esc``/``Tab`` returned focus to the
        Composer (:meth:`action_dismiss_panel`) without answering. Mirrors
        :meth:`on_tabbed_content_tab_activated`'s own focus logic exactly,
        since that only fires on an actual tab-activated Textual message —
        never on a bare "please re-focus what's already active" request like
        this one. An already-answered active tab (nothing left to focus in
        it) falls back to the ``Tabs`` bar itself, still inside this panel
        and Left/Right-navigable to a still-pending sibling tab."""
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        if not active:
            return
        try:
            pane = tabs.get_pane(active)
        except Exception:
            return
        if pane.id in self._answered:
            tabs.query_one(Tabs).focus()
            return
        radios = list(pane.query(RadioSet))
        if radios:
            radios[0].focus()
            return
        inputs = list(pane.query(Input))
        if inputs:
            inputs[0].focus()

    def action_dismiss_panel(self) -> None:
        self.post_message(self.Dismissed())

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(+1)

    def _cycle_tab(self, delta: int) -> None:
        """Move the active tab by ``delta`` positions, wrapping — the
        priority-binding-driven Left/Right cycle (#3308). Cycles through
        EVERY tab (answered or not): reviewing a resolved answer is not
        blocked, only re-answering it is (the form is ``disabled``)."""
        tabs = self.query_one(TabbedContent)
        order = list(self._pane_ids.values())
        if not order:
            return
        current = tabs.active
        idx = order.index(current) if current in order else 0
        tabs.active = order[(idx + delta) % len(order)]


__all__ = ["InterventionPanel"]
