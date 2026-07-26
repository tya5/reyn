"""Phase 3.5 TUI-rebuild gates (#3273): choice-intervention reachability.

These pin the Phase-3.5 fix (ADR self-review finding F1): a closed-set
intervention (permission confirm / choice ``ask_user`` — any
``kind="intervention"`` frame carrying ``meta["choices"]``) is REACHABLE in the
Textual TTY. The free-text-only wiring left choice interventions unanswerable
(the only ``answer_intervention_choice`` caller lived in the dead old app), a
permission-band functional regression this restores.

Also pins the #3290 anti-black-hole fix (part of #3273): a free-text submit
landing during a pending CHOICE intervention must NEVER fall through to
``submit_user_text`` (the backend is blocked waiting for the choice answer, so
that submit is a permanent black hole — 0 events, the UI optimistically lies
that the text was sent). Instead: type-or-click parity — matching text answers
the choice; non-matching text keeps the intervention pending and re-affirms the
options.

Gates:

- **choice REACHABLE** (Tier 2b): a choice-intervention frame surfaces as
  in-flow option chips; a click on a chip delivers the CORRECT ``choice_id``
  through ``transport.answer_intervention_choice`` and the entry re-presents to
  its resolved (``EntryState.SUCCESS``) state. Non-vacuous: the second option is
  chosen (so a first-option shortcut would fail), the specific id is asserted,
  and no answer is delivered before the click.
- **black-hole gone + type-or-click parity** (Tier 2b, #3290): with a choice
  intervention pending, typing an option's label answers that choice via
  ``answer_intervention_choice`` (never ``submit_user_text``); typing a
  non-matching line keeps the intervention pending and surfaces a hint, and
  STILL never reaches ``submit_user_text``.
- **free-text still works** (Tier 2b): a no-choices intervention still routes a
  composer submit to ``answer_intervention_text`` (regression guard).

All use real instances (a concrete recording :class:`ClientTransport`, a real
mounted :class:`TextualChatApp`, real :class:`OutboxMessage`,
:class:`UserIntervention` / :class:`InterventionChoice` heads) — no mocks — per
the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import (
    Composer,
    TextualChatApp,
    choice_chip_spans,
)
from reyn.interfaces.inline.textual_chat.app import _match_choice_input
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.intervention_choices import (
    ALWAYS,
    JUST_PATH,
    NEVER,
    NO,
    YES,
    file_access_choices,
    generic_yn_choices,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.user_intervention import InterventionChoice, UserIntervention

_GUTTER_WIDTH = 2


class _FreeTextHead:
    """A pending free-text intervention head (no ``choices`` attr) — the shape
    the local transport's ``pending_intervention_head`` returns for an
    ``ask_user`` / secret prompt that the composer answers as text."""


class RecordingTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` that replays a fixed frame list
    and RECORDS which answer seam each user action reached.

    ``end=False`` keeps the stream open so the app stays mounted for inspection.
    ``head`` is the pending-intervention head the free-text ``_submit`` path
    reads (``None`` = no pending intervention → a submit is a new turn).
    """

    def __init__(
        self,
        messages: "list[OutboxMessage]",
        *,
        end: bool = False,
        head: "object | None" = None,
    ) -> None:
        self._messages = list(messages)
        self._end = end
        self._head = head
        self.submitted: list[str] = []
        self.answered_choice: list[str] = []
        self.answered_text: list[str] = []
        self.displayed: list[OutboxMessage] = []

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
        self.answered_text.append(text)
        return True

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        self.answered_choice.append(choice_id)
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return self._head

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


def _choice_head() -> UserIntervention:
    """A real pending CHOICE-intervention head (Yes / No / Always), matching the
    ``choices`` of :func:`_choice_intervention`. This is the shape
    ``transport.pending_intervention_head`` returns for a permission confirm —
    a real :class:`UserIntervention`, not a stand-in, so ``_submit`` reads the
    same ``.choices`` (``.id`` / ``.label`` / ``.hotkey``) production reads."""
    return UserIntervention(
        kind="permission.write",
        prompt="Allow write to /etc/hosts?",
        choices=[
            InterventionChoice(id="yes", label="Yes", hotkey="y"),
            InterventionChoice(id="no", label="No", hotkey="n"),
            InterventionChoice(id="always", label="Always", hotkey="A"),
        ],
    )


def _iv_entry(app: TextualChatApp):
    entries = [
        e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
    ]
    assert len(entries) == 1, f"expected one intervention entry, got {len(entries)}"
    return entries[0]


@pytest.mark.asyncio
async def test_choice_intervention_click_delivers_correct_choice_id() -> None:
    """Tier 2b: a choice-intervention frame is REACHABLE — clicking its second
    option chip delivers that option's ``choice_id`` ("no") through
    ``answer_intervention_choice`` and the entry goes to its resolved SUCCESS
    state. This is the F1 permission-band reachability witness. Non-vacuous: the
    SECOND option is chosen and its exact id asserted (a first-option or
    label-vs-id confusion fails), and no answer is delivered before the click."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        flow = app.query_one(FlowView)

        # No answer is delivered until the user acts.
        assert transport.answered_choice == []

        body_width = max(1, flow.scrollable_content_region.width - _GUTTER_WIDTH)
        chip_row = app._presenter.choice_chip_row(entry.item, body_width)
        spans = choice_chip_spans(entry.item.meta["choices"])
        # Choose the SECOND chip ("No", id "no") — proves the click maps to the
        # right choice, not merely "some choice was answered".
        start, end, choice_id = spans[1]
        assert choice_id == "no"
        click_x = (start + end) // 2
        app.post_message(FlowView.Clicked(flow, entry, click_x, chip_row))
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["no"], (
            f"choice not delivered; got {transport.answered_choice}"
        )
        # Resolved reflection: green SUCCESS gutter + recorded chosen label.
        resolved = _iv_entry(app)
        assert resolved.state is EntryState.SUCCESS
        assert resolved.item.meta.get("_chosen_label") == "No"


@pytest.mark.asyncio
async def test_choice_click_off_the_chip_row_does_not_answer() -> None:
    """Tier 2b: hit-testing is non-vacuous — a click on the prompt HEAD row (not
    the chip row) delivers nothing, so the reachability test's positive result is
    attributable to landing on a chip, not to any click resolving the whole
    entry."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        flow = app.query_one(FlowView)
        # Row 0 is the prompt head, never the chip row (the head is one line here).
        app.post_message(FlowView.Clicked(flow, entry, 3, 0))
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_choice == []


@pytest.mark.asyncio
async def test_choice_free_text_match_answers_choice_not_new_turn() -> None:
    """Tier 2b: type-or-click parity (#3290) — with a choice-intervention
    pending, typing an option's LABEL ("yes", case-insensitive) answers the Yes
    chip via ``answer_intervention_choice`` and NEVER starts a new turn.

    Non-vacuous (black-hole gone): the pre-fix code fell straight through to
    ``submit_user_text`` for ANY choice-head submit — the #3290 black hole
    (backend blocked on the choice, 0 events). Asserting ``submitted == []`` is
    exactly what neutering the new choice branch (reverting to the fall-through)
    flips RED. The delivered id is the Yes chip's ``id`` ("yes"), not the first
    chip index nor the label string — a label-as-id confusion fails."""
    transport = RecordingTransport(
        [_choice_intervention()], end=False, head=_choice_head()
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("y", "e", "s")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.answered_choice == ["yes"], (
        f"typed option not routed to the choice; got {transport.answered_choice}"
    )
    assert transport.submitted == []  # never black-holed into a new turn
    assert transport.answered_text == []


@pytest.mark.asyncio
async def test_choice_free_text_no_match_keeps_pending_and_hints() -> None:
    """Tier 2b: the anti-black-hole no-match leg (#3290) — with a choice
    intervention pending, a free-text line that matches NO option does NOT reach
    ``submit_user_text`` (the black hole); it surfaces a hint and keeps the
    intervention pending so the user can still click a chip or retype.

    Non-vacuous: ``submitted == []`` is what the pre-fix fall-through violated,
    and a ``kind="system"`` hint frame is asserted present — so the input was
    neither delivered as a choice nor dropped silently."""
    transport = RecordingTransport(
        [_choice_intervention()], end=False, head=_choice_head()
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.submitted == []  # #3290: not black-holed into a blocked backend
    assert transport.answered_choice == []
    assert transport.answered_text == []
    hints = [m for m in transport.displayed if m.kind == "system"]
    assert hints, "no re-affirm hint surfaced for the unmatched choice submit"


@pytest.mark.asyncio
async def test_slash_command_during_choice_intervention_takes_normal_path() -> None:
    """Tier 2b: the ``/``-guard survives (#3290 regression) the restructured
    branch — a ``/``-prefixed line submitted while a CHOICE intervention is
    pending is a slash command, so it routes to ``submit_user_text`` (the session
    turn loop dispatches it), NOT to the choice-answer seam nor the hint."""
    transport = RecordingTransport(
        [_choice_intervention()], end=False, head=_choice_head()
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("slash", "m", "o", "d", "e", "l")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.submitted == ["/model"]
    assert transport.answered_choice == []
    assert transport.answered_text == []


def test_match_choice_input_label_hotkey_case_and_ambiguity() -> None:
    """Tier 1: the ``_match_choice_input`` contract — trimmed, case-insensitive
    match against BOTH label and hotkey; the ``choice_id`` (never itself matched)
    is returned; ambiguity and no-match both return ``None``."""
    choices = [
        InterventionChoice(id="yes", label="Yes", hotkey="y"),
        InterventionChoice(id="no", label="No", hotkey="n"),
        InterventionChoice(id="always", label="Always", hotkey="A"),
    ]
    # Label match, case-insensitive + trimmed.
    assert _match_choice_input("yes", choices) == "yes"
    assert _match_choice_input("  YES  ", choices) == "yes"
    # Hotkey match, case-insensitive (hotkey "A" for Always).
    assert _match_choice_input("a", choices) == "always"
    # The id itself is NOT a match key (would confuse label-vs-id).
    assert _match_choice_input("no", choices) == "no"
    # No match / empty → None (caller keeps pending + hints, never black-holes).
    assert _match_choice_input("maybe", choices) is None
    assert _match_choice_input("   ", choices) is None
    # Ambiguity → None: two distinct choices sharing the same label.
    dup = [
        InterventionChoice(id="a", label="Go", hotkey="g"),
        InterventionChoice(id="b", label="Go", hotkey="h"),
    ]
    assert _match_choice_input("go", dup) is None


def test_match_choice_input_full_word_matches_bracket_decorated_label() -> None:
    """Tier 1: #3290 follow-up — bracket-decorated labels (the real
    ``generic_yn_choices()`` / ``file_access_choices()`` shape, e.g.
    ``"[y]es"``) must resolve on the FULL WORD (``"yes"``), not only the
    hotkey. Non-vacuity: the OLD exact-match (raw label + hotkey only) would
    have failed ``"yes"`` against ``"[y]es"`` (``"yes" != "[y]es"`` and
    ``"yes" != "y"``) — this proves the de-decoration candidate actually
    changed the outcome, not just added a redundant path."""
    choices = generic_yn_choices()  # [y]es / [A]lways / [n]o / [N]ever

    # OLD behavior would have returned None here (falsifies vacuity).
    old_exact_match_candidates = {
        str(v).strip().casefold()
        for c in choices
        for v in (c.label, c.hotkey)
    }
    assert "yes" not in old_exact_match_candidates

    # NEW behavior: full word resolves via the de-decorated candidate.
    assert _match_choice_input("yes", choices) == YES
    assert _match_choice_input("no", choices) == NO
    assert _match_choice_input("always", choices) == ALWAYS
    assert _match_choice_input("never", choices) == NEVER
    # Hotkeys still resolve (regression guard — existing candidates kept).
    # ("n" is skipped here: NO's hotkey "n" and NEVER's hotkey "N" casefold to
    # the same needle, a pre-existing case-insensitive hotkey collision in the
    # 4-choice generic_yn set that predates and is unrelated to this fix.)
    assert _match_choice_input("y", choices) == YES
    # Trimmed + case-insensitive, same as the raw-label path.
    assert _match_choice_input("  YES  ", choices) == YES
    # Unrelated word still returns None (stays pending, hints).
    assert _match_choice_input("maybe", choices) is None

    # A longer decorated label ("[j]ust this path always") also de-decorates.
    file_choices = file_access_choices("/some/dir")
    assert _match_choice_input("just this path always", file_choices) == JUST_PATH
    assert _match_choice_input("yes", file_choices) == YES


def test_match_choice_input_standard_yn_set_no_new_collision() -> None:
    """Tier 1: #3290 follow-up ambiguity guard — de-decorating the standard
    yes/no/always/never label set must NOT introduce a new cross-choice
    collision (each de-decorated word is distinct), so each still resolves to
    exactly one id rather than falling to ``None`` via the ambiguity guard."""
    choices = generic_yn_choices()
    resolved = {
        word: _match_choice_input(word, choices)
        for word in ("yes", "no", "always", "never")
    }
    assert resolved == {
        "yes": YES,
        "no": NO,
        "always": ALWAYS,
        "never": NEVER,
    }
    assert len(set(resolved.values())) == 4  # all four distinct, none collided to None


@pytest.mark.asyncio
async def test_free_text_intervention_still_answered_via_composer() -> None:
    """Tier 2b: regression — a FREE-TEXT intervention (no choices) still routes a
    composer submit to ``answer_intervention_text``, unchanged by the choice
    wiring. The head has no ``choices`` attr, so ``_submit`` takes the answer
    path."""
    transport = RecordingTransport([], end=False, head=_FreeTextHead())
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("o", "k")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.answered_text == ["ok"]
    assert transport.submitted == []
    assert transport.answered_choice == []


def test_choice_labels_are_neutralized_before_rendering() -> None:
    """Tier 2c: an LLM-derived choice LABEL carrying raw terminal control
    sequences is neutralized before it reaches the rendered chip cells — a
    terminal-escape-injection guard on a permission surface.

    Choice labels reach ``meta["choices"]`` RAW (``session._iv_meta`` copies
    ``choice.label`` verbatim; only the ``nodes`` render-model is neutralized at
    source), so the presenter MUST strip control bytes at its own boundary. This
    builds a choice-intervention whose label embeds a CSI colour + OSC
    title-set + bare ESC/BEL, presents it through the real
    ``_present_intervention_choice`` path, renders the presentation through a
    no-colour Console (so any escape in the output came from the LABEL, not from
    Rich styling), and asserts the rendered cells carry NO raw ``\\x1b`` / ``\\x07``
    while the visible label text survives (neutralized, not dropped).

    NON-VACUITY (falsification): neutering ``presenter._neutralized_label`` to
    identity (the future refactor the co-vet flagged) makes the raw ``\\x1b`` leak
    into the rendered cells and flips this assertion RED — verified locally, so a
    silent removal of the neutralization is caught here."""
    from rich.console import Console

    from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter

    payload = "\x1b[31mDANGER\x1b]0;pwn\x07"
    msg = OutboxMessage(
        kind="intervention",
        text="Allow write?",
        meta={
            "intervention_id": "iv-x",
            "intervention_kind": "confirm",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": payload, "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )

    presentation = ReynPresenter()._present_intervention_choice(msg, 80)
    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "\x1b" not in rendered, f"raw ESC leaked into chip cells: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into chip cells: {rendered!r}"
    # The visible label survives — neutralization strips the control bytes, it
    # does not drop the option (a dropped option would be its own regression).
    assert "DANGER" in rendered
