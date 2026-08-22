"""#3354 — the composer's ``/``-command and ``:``-skill completion menu.

The retired prompt_toolkit inline app completed both namespaces in its input
box; #3273 Phase 6 deleted that app and never re-wired the UI, so the menu was
gone while every SUPPLIER survived (``slash_command_completions``, the per-command
``CompleterFn``, ``skill_invoke_completions``). These tests guard the re-wiring,
and specifically the parts that are easy to get vacuously right:

- the command→argument TRANSITION, not just one snapshot: a menu that shows
  command names for ``/image`` and keeps showing them after the space would look
  fine in a terminal-state assertion and be exactly the behaviour the retired
  implementation was careful to avoid.
- ``↑`` witnessed in BOTH states — moving the menu highlight while open, and
  reaching the sent-queue while closed. Either alone proves nothing about the
  routing decision.
- the menu opening on TYPING and not on buffer contents (a programmatic write
  must not pop a menu the user never asked for).
- the ``:`` trigger's two independent gates (word boundary AND length).

#3364 extended the same menu with the argument stage's ``↳ usage:`` header row —
the surface ``SlashCommand.usage`` was added for and no code ever read. Its tests
live here rather than in a file of their own because they need this file's
harness (real app + real transport + real session) and they guard the SAME
widget; the hazards they add are the header's two structural risks: an
informational row silently becoming the Tab target (an off-by-one accept), and
an informational row claiming keys the user still needs.

#3545 added the row-height tests at the end, on the same grounds and against the
same widget: a skill description does not fit on one line, and what can go wrong
is invisible to a "the menu opened" assertion — a row landing at column 0 where
the next candidate's own ``:name`` starts, so three rows read as six.

#3551 (owner ruling A) changed the mechanism from a hanging-indent wrap to a
one-line clip, so those tests now pin the clip. The INVARIANT is unchanged and is
what they assert: no row may exceed the width, because an overflowing row is
re-wrapped by Textual itself at column 0 and #3545's symptom returns. It is
asserted in CELLS against a fixture carrying wide characters — a clip written
with ``len`` passes a character-counting check and still overflows a terminal.
The structural test is unchanged: mounting each visual line as its OWN option
would satisfy the visual checks while breaking ``↓``, the highlight and the Tab
accept.

Real ``TextualChatApp`` + real ``ClientTransport`` + real ``Session`` (via the
shared ``tests._support.agent_session.make_session``) + real ``SkillEntry``
dataclasses, and real suppliers throughout — no mocks, no hand-rolled
stand-ins. A substituted candidate list is the specific hazard here: it would
make the whole feature look tested while wired to nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from rich.cells import cell_len

from reyn.core.events.state_log import StateLog
from reyn.data.skills.registry import SkillEntry
from reyn.interfaces.inline.textual_chat import Composer, MenuBar, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import COMPOSER_KEYS
from reyn.interfaces.inline.textual_chat.completion import (
    KIND_ARGUMENT,
    KIND_COMMAND,
    KIND_NONE,
    KIND_SKILL,
    NO_MATCH_ROW,
    ROW_ELLIPSIS,
    USAGE_ROW_PREFIX,
    CompletionPopup,
    compute_completion,
)
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    ChatReadModel,
    completion_source_snapshot_from_session,
)
from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.schemas.models import Event
from tests._support.agent_session import make_session

# ── real collaborators ───────────────────────────────────────────────────────


class RecordingTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` (same shape as
    ``test_3327_keyboard_reachability_to_panel.RecordingTransport``): stays open
    on a live queue so a test can push a ``user_submitted`` event to populate the
    sent-queue, and records what was submitted."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.submitted: "list[str]" = []
        # #3595 S5: the client interprets a ``/`` line itself, so what it shows
        # is the observable for a command that did or did not resolve.
        self.displayed: "list" = []

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        return "m-" + str(len(self.submitted))

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return True

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:
        self.displayed.append(msg)

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class SessionReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (same pattern as
    ``test_3338``'s ``_MutableSnapshotReadModel``) that holds a REAL
    ``Session`` — the seam the app resolves both completion sources
    through. :meth:`completion_source` converts it to a
    :class:`CompletionSourceSnapshot` VALUE (#5044) via the SAME shared
    helper :class:`RegistryReadModel` uses in production
    (:func:`completion_source_snapshot_from_session`), never handing the
    live ``Session`` itself across the seam — this test double exercises
    the real conversion, not a shortcut around it."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, session=None) -> None:
        self._session = session

    def completion_source(self):
        if self._session is None:
            return None
        return completion_source_snapshot_from_session(self._session)

    def snapshot(self, config=None):
        return None

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return False

    @property
    def history_path(self) -> Path:
        return Path("/nonexistent/.input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


def _real_session(tmp_path: Path, *, skills=None):
    """A real ``Session`` with ``skills`` registered — the object a
    ``CompleterFn`` is called with and the ``:`` source is read from."""
    from reyn.runtime.session_params import CapabilityScope

    return make_session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=list(skills or []) or None),
    )


def _user_submitted(*, msg_id: str, text: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": "c1", "msg_id": msg_id, "seq": seq, "meta": {}},
    )


def _labels(popup: CompletionPopup) -> "list[str]":
    """The command/skill token of each displayed row (the row's first field),
    read off the MOUNTED options rather than recomputed."""
    return [row.split("  ")[0] for row in popup.rendered_rows()]


# ── the command → argument transition ────────────────────────────────────────


def test_command_candidates_stop_when_the_argument_stage_begins(tmp_path, monkeypatch):
    """Tier 2b: the TRANSITION — ``/ima`` offers COMMAND names, and the moment
    the command word is settled by a space the command candidates STOP and
    ``/image``'s own ``CompleterFn`` supplies ARGUMENT candidates instead.

    Driven through the real ``_image_path_completer`` against a real directory,
    so the argument candidates are genuinely supplier-derived. Asserting only
    the second snapshot would pass for an implementation that never stopped
    offering commands — the exact behaviour the retired ``_SlashCompleter``
    documented as its reason for going quiet."""
    (tmp_path / "shot.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _real_session(tmp_path)

    source = completion_source_snapshot_from_session(session)
    before = compute_completion("/ima", source=source)
    assert before.kind == KIND_COMMAND
    assert "/image" in [c.label for c in before.candidates], (
        f"the command stage did not offer /image; got {[c.label for c in before.candidates]}"
    )

    after = compute_completion("/image ", source=source)
    assert after.kind == KIND_ARGUMENT, (
        "typing the space did not move the menu to the argument stage — the "
        "command word is settled, so command candidates must stop"
    )
    values = [c.value for c in after.candidates]
    assert "/image" not in values and "image" not in values, (
        f"command candidates survived into the argument stage: {values}"
    )
    assert "shot.png" in values, (
        f"the real _image_path_completer's candidates are missing: {values}"
    )
    assert "notes.txt" not in values, (
        "the argument stage is not going through the real completer — "
        "_image_path_completer filters to image extensions, so a .txt file "
        f"must not appear: {values}"
    )


def test_argument_candidates_filter_by_the_last_typed_word(tmp_path, monkeypatch):
    """Tier 2b: an argument prefix narrows the candidates, and ``prefix_len``
    covers only that word — so accepting replaces the partial argument, never
    the command name in front of it."""
    (tmp_path / "shot.png").write_bytes(b"x")
    (tmp_path / "banner.png").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    session = _real_session(tmp_path)

    source = completion_source_snapshot_from_session(session)
    state = compute_completion("/image sh", source=source)
    assert [c.value for c in state.candidates] == ["shot.png"]
    assert state.prefix_len == len("sh"), (
        "prefix_len must cover only the argument word being completed — a "
        "larger span would eat the command name on accept"
    )


def test_model_argument_completion_uses_the_configured_model_classes(tmp_path):
    """Tier 2b: ``/model `` completes from the session's OWN
    ``known_model_classes()`` — the same list the command's no-arg branch
    prints and would validate an override against, so the menu cannot offer a
    class the command would then reject.

    (#3354 finding: ``/model`` carried NO ``CompleterFn`` before this change,
    despite being the command most likely to want argument help. The completer
    added here wires the existing accessor; it introduces no new source.)"""
    session = _real_session(tmp_path)
    expected = session.known_model_classes()
    assert expected, "test setup: the session reports no configured model classes"

    source = completion_source_snapshot_from_session(session)
    state = compute_completion("/model ", source=source)
    assert state.kind == KIND_ARGUMENT
    assert [c.value for c in state.candidates] == list(expected), (
        f"/model completion diverged from known_model_classes(): "
        f"{[c.value for c in state.candidates]} vs {expected}"
    )


# ── the `:` namespace, through the real supplier ─────────────────────────────


def _skill_entries():
    """Real ``SkillEntry`` dataclasses — cheaply constructible, so the policy
    requires the real type rather than a stand-in carrying invented fields."""
    return [
        SkillEntry(name="review", description="Review a pull request", path="a.md"),
        SkillEntry(name="refactor", description="Refactor a module", path="b.md"),
        SkillEntry(
            name="reindex", description="Rebuild the index", path="c.md",
            visibility="hidden",
        ),
        SkillEntry(
            name="retire", description="Retire a flag", path="d.md", enabled=False,
        ),
    ]


def test_skill_completion_goes_through_the_real_visibility_filter():
    """Tier 2b: ``:`` candidates come from the real
    :func:`~reyn.interfaces.skill_invoke.skill_invoke_completions`, which is
    what makes them obey the SAME ``menu``/``on_demand``/``hidden`` surface the
    ``:name`` INVOCATION path enforces.

    The hidden and disabled entries are the non-vacuity witnesses: a
    substituted candidate list (or a re-implemented prefix filter) would offer
    all four, suggesting a skill that invocation would then refuse."""
    state = compute_completion(":re", skills=_skill_entries())

    assert state.kind == KIND_SKILL
    names = [c.value for c in state.candidates]
    assert set(names) == {"review", "refactor"}, (
        f"expected only the invocable menu/on_demand skills; got {names}"
    )
    assert "reindex" not in names, (
        "a `hidden` skill was offered — the real skill_invoke_completions "
        "filter is not in the path, so completion would suggest a name the "
        "invocation path refuses"
    )
    assert "retire" not in names, "a disabled skill was offered"


def test_skill_completion_surfaces_each_candidates_description():
    """Tier 2b: the ``:`` menu shows each candidate's description, not just its
    name (the retired completer's ``display_meta``; issue requirement 3)."""
    state = compute_completion(":rev", skills=_skill_entries())
    rows = state.rows()
    assert any("Review a pull request" in row for row in rows), (
        f"no candidate description reached the rendered row: {rows}"
    )


@pytest.mark.parametrize(
    "text,should_trigger,why",
    [
        (":", True, "a bare line-initial colon lists everything, like a bare /"),
        (":r", True, "one char at line start is enough — no length gate there"),
        (":re", True, "colon at input start with 2+ chars"),
        ("run :re", True, "colon after whitespace with 2+ chars"),
        ("run :", False, "mid-line keeps the length gate"),
        ("run :r", False, "mid-line keeps the length gate at 1 char"),
        ("http://xx", False, "colon mid-word fails the word-boundary gate"),
        ("12:30", False, "a time fails the word-boundary gate"),
        ("ratio:20", False, "colon mid-word fails the word-boundary gate"),
        ("note: se", False, "a space after the colon is not a skill token"),
        ("note: x", False, "colon follows `e`, so the word boundary rejects it"),
        ("http://x", False, "colon mid-word fails the word-boundary gate"),
        ("ratio:2", False, "colon mid-word fails the word-boundary gate"),
        ("note: see below", False, "colon follows `e`, and a space follows it"),
    ],
)
def test_skill_trigger_is_word_boundary_plus_a_midline_only_length_gate(
    text, should_trigger, why,
):
    """Tier 1: the ``:`` trigger contract after #3541 — the word boundary is
    unconditional, the length gate applies MID-LINE only.

    The five counterexamples the module docstring names (``http://x``,
    ``12:30``, ``ratio:2``, ``note: see below``, ``note: x``) are all here and
    all stay quiet: each has a non-space character immediately before its colon,
    so the WORD-BOUNDARY rule alone rejects them and dropping the length gate at
    line start cannot revive any of them. ``run :`` / ``run :r`` are the
    mid-line half — the case no counterexample covers, where the floor is kept.
    """
    state = compute_completion(text, skills=_skill_entries())
    triggered = state.kind != KIND_NONE
    assert triggered is should_trigger, (
        f"{text!r}: expected trigger={should_trigger} ({why}), got kind={state.kind!r}"
    )


def test_bare_line_initial_colon_offers_every_invocable_skill():
    """Tier 1: ``:`` with nothing after it opens the menu on the FULL invocable
    list — the owner-reported symmetry with a bare ``/`` (#3541).

    Asserting the candidate SET, not just that the menu opened: a trigger that
    fired while passing the wrong prefix through would still be `is_open` but
    would offer nothing."""
    state = compute_completion(":", skills=_skill_entries())

    assert state.kind == KIND_SKILL, "a bare `:` did not open the skill menu"
    assert {c.value for c in state.candidates} == {"review", "refactor"}, (
        f"a bare `:` must offer every invocable skill; got "
        f"{[c.value for c in state.candidates]}"
    )


@pytest.mark.parametrize(
    "text,expected_prefix_len,expected_token_start",
    [
        (":", 0, 1),
        (":r", 1, 1),
        (":re", 2, 1),
        ("run :re", 2, 5),
        ("a b :ref", 3, 5),
    ],
)
def test_skill_token_offsets_hold_on_both_the_line_start_and_midline_branches(
    text, expected_prefix_len, expected_token_start,
):
    """Tier 1: ``prefix_len`` and ``token_start`` are the ACCEPT contract — the
    composer replaces exactly ``prefix_len`` characters ending at the cursor and
    keys its sticky dismissal on ``token_start``.

    Both branches are covered because they are matched by two different
    patterns: a shape that folded them into one two-alternative regex would
    shift the capture-group number, and a wrong ``token_start`` corrupts the
    accepted text silently rather than raising."""
    state = compute_completion(text, skills=_skill_entries())

    assert state.kind == KIND_SKILL, f"{text!r} did not trigger"
    assert state.prefix_len == expected_prefix_len, (
        f"{text!r}: prefix_len must cover the typed name without the sigil"
    )
    assert state.token_start == expected_token_start, (
        f"{text!r}: token_start must point at the first name character, i.e. "
        f"just past the `:`"
    )
    assert text[state.token_start:] == text[len(text) - state.prefix_len:], (
        f"{text!r}: token_start and prefix_len disagree about which characters "
        f"the accept path would replace"
    )


# ── source availability vs. no matches (remote stays SILENT) ─────────────────


def test_unreadable_sources_stay_silent_rather_than_showing_an_empty_menu():
    """Tier 2b: a client that cannot read a namespace's source shows NOTHING —
    never an empty-looking menu, which would read as "no such command exists".

    This is the remote ``--connect`` case: no local ``Session``, so no
    ``CompleterFn`` can be called and no skill list can be enumerated.

    The line the rule is drawn on is the SOURCE, not the stage: what a remote
    client cannot compute stays absent, and what it can compute — anything
    REGISTRY-derived, which is command names and (since #3364) the argument
    stage's usage header — keeps working. So ``/image `` on a remote client
    offers no filesystem candidates and no ``NO_MATCH_ROW`` (nothing was asked),
    but still says what ``/image`` takes."""
    remote_arg = compute_completion("/image ", source=None)
    assert remote_arg.candidates == (), (
        "a session-less client produced argument candidates from somewhere"
    )
    assert NO_MATCH_ROW not in remote_arg.rows(), (
        "a session-less client claimed the user's argument matched nothing, when "
        f"no completer was ever called: {remote_arg.rows()}"
    )
    assert not remote_arg.owns_keys, (
        "a menu with nothing to navigate claimed the navigation keys"
    )
    assert compute_completion("/agents ", source=None).kind == KIND_NONE, (
        "a command with neither an argument source nor a usage line must be "
        "silent, not an empty menu"
    )
    assert compute_completion(":re", skills=None).kind == KIND_NONE, (
        "skill completion must be silent when the skill source is unavailable"
    )
    # But COMMAND-name completion is registry-derived and works everywhere.
    remote = compute_completion("/im", source=None, skills=None)
    assert remote.kind == KIND_COMMAND and remote.candidates, (
        "command-name completion must keep working on a client with no session"
    )


def test_readable_but_empty_source_shows_an_explicit_no_match_row():
    """Tier 2b: the OTHER side of the distinction above — a source that IS
    readable and simply matched nothing keeps the menu open with an explicit
    row, so the user can tell "your prefix matches nothing" from "no menu
    exists here"."""
    state = compute_completion(":zzz", skills=_skill_entries())
    assert state.kind == KIND_SKILL, "a readable source must still trigger"
    assert state.candidates == ()
    assert state.rows() == [NO_MATCH_ROW]


# ── key routing, witnessed in BOTH states ───────────────────────────────────


@pytest.mark.asyncio
async def test_up_moves_the_menu_highlight_while_open(tmp_path) -> None:
    """Tier 2b: with the menu OPEN, ``↑``/``↓`` move its highlight.

    Half of the two-state witness — see the companion test below for the same
    key with the menu CLOSED. Multi-row by construction (bare ``/`` lists the
    registry): a single-row menu would clamp at 0 and the movement assertion
    would pass vacuously."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        assert popup.is_open, "typing / did not open the menu"
        assert len(popup.rendered_rows()) > 1, (
            "test setup: need a multi-row menu for the movement assertion to "
            "mean anything"
        )
        first = popup.selected()

        await pilot.press("down")
        await pilot.pause()
        second = popup.selected()
        assert second is not None and second != first, (
            f"↓ did not move the menu highlight ({first} -> {second})"
        )

        await pilot.press("up")
        await pilot.pause()
        assert popup.selected() == first, "↑ did not move the highlight back"
        assert composer.has_focus, (
            "focus left the composer — the popup must stay non-focusable so the "
            "composer remains the single key owner"
        )


@pytest.mark.asyncio
async def test_up_reaches_the_sent_queue_while_the_menu_is_closed(tmp_path) -> None:
    """Tier 2b: with the menu CLOSED, ``↑`` keeps its #3300/#3314 routing and
    focuses the non-empty sent-queue.

    The other half of the two-state witness: together these show the SAME key
    going to two different places depending on menu state, which is the whole
    routing decision. Either alone would be consistent with a key that always
    goes to one of them."""
    from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", text="queued while busy", seq=1)
        )
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        sent_queue = app.query_one(SentQueue)
        composer = app.query_one(Composer)
        assert sent_queue.has_items(), "test setup: sent-queue must be non-empty"
        assert not popup.is_open, "test setup: the menu must be closed"
        composer.focus()
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert sent_queue.has_focus, (
            "↑ with the menu closed did not reach the sent-queue — the "
            "pre-#3354 routing regressed"
        )


@pytest.mark.asyncio
async def test_every_close_path_releases_the_arrow_keys(tmp_path) -> None:
    """Tier 2b: after the menu closes, ``↑`` must reach the sent-queue again —
    witnessed on ALL THREE close paths (``Escape``, accepting a candidate,
    deleting the trigger).

    A menu that closes visually but keeps claiming the arrows is the #3327
    keyboard-deadlock class: the user sees no menu and cannot understand why
    ``↑`` does nothing. One close path passing does not cover the others —
    each releases through a different code path (``dismiss_current`` /
    ``close`` via accept / ``sync`` computing a closed state)."""
    from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_user_submitted(msg_id="m1", text="queued", seq=1))
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        sent_queue = app.query_one(SentQueue)
        composer = app.query_one(Composer)
        assert sent_queue.has_items(), "test setup: sent-queue must be non-empty"

        async def _open_menu() -> None:
            composer.clear_and_reset()
            composer.focus()
            await pilot.pause()
            await pilot.press("slash", "h", "e")
            await pilot.pause()
            assert popup.is_open, "test setup: the menu did not open"

        async def _assert_arrow_released(close_path: str) -> None:
            assert not popup.is_open, f"{close_path}: the menu is still open"
            composer.focus()
            await pilot.pause()
            composer.move_cursor((0, 0))
            await pilot.press("up")
            await pilot.pause()
            assert sent_queue.has_focus, (
                f"{close_path}: the menu closed but ↑ still did not reach the "
                f"sent-queue — the arrow was never released"
            )

        # Path 1 — Escape.
        await _open_menu()
        await pilot.press("escape")
        await pilot.pause()
        await _assert_arrow_released("escape")

        # Path 2 — accepting a candidate.
        await _open_menu()
        await pilot.press("tab")
        await pilot.pause()
        await _assert_arrow_released("accept")

        # Path 3 — deleting the trigger character.
        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert popup.is_open, "test setup: bare / did not open the menu"
        await pilot.press("backspace")
        await pilot.pause()
        await _assert_arrow_released("trigger deleted")


@pytest.mark.asyncio
async def test_tab_accepts_and_enter_still_submits(tmp_path) -> None:
    """Tier 2b: ``Tab`` accepts the highlighted candidate; ``Enter`` submits
    what the user actually typed even with the menu open.

    Enter is deliberately NOT an accept key: with Enter as send, accepting on
    Enter would silently swap a fully-typed command for whichever row happened
    to be highlighted. The submitted text below is the typed prefix, not the
    highlighted candidate — that is the point."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "h", "e")
        await pilot.pause()
        assert popup.is_open
        await pilot.press("tab")
        await pilot.pause()
        assert composer.text == "/help ", (
            f"Tab did not accept the candidate; composer holds {composer.text!r}"
        )
        assert not popup.is_open, "accepting must close the menu"

        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        assert popup.is_open, "test setup: the menu must be open for this half"
        await pilot.press("enter")
        await pilot.pause()

        # #3595 S5: a ``/`` line is INTERPRETED by the client rather than
        # submitted as a turn, so what the typed text reached is the command
        # layer. ``/he`` resolves to nothing, and the error names what the user
        # actually typed — which is the discriminator this test is about: had
        # Enter accepted the highlighted row, ``/help`` would have RUN.
        shown = " ".join(m.text for m in transport.displayed)
        assert "unknown command /he;" in shown, (
            "Enter with the menu open must act on the TYPED text, not accept "
            f"the highlighted candidate; the client showed {shown!r}"
        )
        assert transport.submitted == [], (
            f"a command line was also submitted as a turn: {transport.submitted}"
        )


@pytest.mark.asyncio
async def test_tab_still_cycles_focus_to_the_menubar_while_the_menu_is_closed(
    tmp_path,
) -> None:
    """Tier 2b: with the menu CLOSED, ``Tab`` keeps its #3277 meaning and moves
    focus from the composer to the :class:`MenuBar`.

    The closed-state half of the ``Tab`` witness (its open-state half is
    ``test_tab_accepts_and_enter_still_submits``). Without this pair, an
    implementation that claimed ``Tab`` UNCONDITIONALLY would pass every other
    test in this file — and ``Tab`` is the worst key to leave uncovered, because
    it is the route to a DIFFERENT feature: #3277's composer→MenuBar focus
    cycling, which a user reaches without ever opening a completion menu. A
    regression there breaks navigation for someone who never types ``/``.

    ``Tab`` is not reyn's to give away: ``TextArea`` defaults to
    ``tab_behavior="focus"``, so the key bubbles to ``Screen``'s
    ``Binding("tab", "app.focus_next")``. The completion menu BORROWS it while
    open; this pins that the borrow ends when the menu closes."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        menubar = app.query_one(MenuBar)
        composer.focus()
        await pilot.pause()

        assert not popup.is_open, "test setup: the menu must be closed"
        assert composer.has_focus, "test setup: the composer must start focused"

        await pilot.press("tab")
        await pilot.pause()

        assert menubar.has_focus, (
            "Tab with the menu closed did not reach the MenuBar — #3277's "
            "composer→menu focus cycling regressed. The completion menu must "
            "only borrow Tab while it is OPEN"
        )
        assert not composer.has_focus, "focus did not leave the composer"

        # Non-vacuity: the SAME key in the SAME app accepts a candidate while
        # the menu is open, so the assertion above is not passing because Tab is
        # inert here.
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        assert popup.is_open, "test setup: the menu did not open"
        await pilot.press("tab")
        await pilot.pause()
        assert composer.text == "/help ", (
            f"Tab did not accept while open; composer holds {composer.text!r}"
        )
        assert composer.has_focus, (
            "accepting a candidate moved focus off the composer — Tab must not "
            "also cycle focus when it is being used to accept"
        )


@pytest.mark.asyncio
async def test_escape_dismissal_is_sticky_for_that_token(tmp_path) -> None:
    """Tier 2b: after ``Esc``, typing MORE of the same token does not reopen the
    menu; a genuinely FRESH trigger does.

    Without the sticky half, ``Esc`` is useless — the menu returns on the next
    keystroke and keeps eating ``↑``. Without the re-arm half, one ``Esc``
    would disable completion for the rest of the session."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "m", "o")
        await pilot.pause()
        assert popup.is_open
        await pilot.press("escape")
        await pilot.pause()
        assert not popup.is_open

        await pilot.press("d")
        await pilot.pause()
        assert composer.text == "/mod", "test setup: the keystroke did not land"
        assert not popup.is_open, (
            "typing more of a dismissed token reopened the menu — Esc did not "
            "stick, so it cannot be used to get the arrows back"
        )

        # A fresh trigger re-arms: clear and start a new token.
        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "h")
        await pilot.pause()
        assert popup.is_open, (
            "a fresh / token did not reopen the menu — one Esc must not disable "
            "completion for the rest of the session"
        )


@pytest.mark.asyncio
async def test_menu_opens_on_typing_not_on_buffer_contents(tmp_path) -> None:
    """Tier 2b: the menu is a response to TYPING. A programmatic write that
    leaves a would-be trigger behind the caret opens nothing.

    This is what keeps the #3300 Y-client cancelled-text restore (which puts a
    cancelled ``/command`` back into the box AND positions the cursor after it)
    from popping a menu the user never asked for — and it must hold
    structurally, not just at that one call site.

    **The write must MOVE THE CURSOR to be a real witness.** A bare
    ``composer.text = "/model"`` resets the caret to ``(0, 0)`` (measured), so
    ``text_before_cursor()`` is empty and NOTHING would trigger regardless of
    this gate — an assertion on that shape passes even with the gate stripped
    out, which is exactly how it was caught here. The write below therefore
    mirrors the restore's real shape: text, then cursor placed after it.

    Deliberately NOT routed through ``_restore_cancelled_text``: that path
    ALSO closes the popup explicitly, so it would keep this assertion green
    with the typing gate removed (two protections cross-masking one another).
    This exercises the gate alone."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        composer.text = "/model"
        composer.move_cursor((0, len("/model")))
        await pilot.pause()
        await pilot.pause()

        assert composer.text_before_cursor() == "/model", (
            "test setup: the write must leave the trigger BEHIND the caret, or "
            "this proves nothing about the typing gate"
        )
        assert not popup.is_open, (
            "a programmatic write opened the completion menu — the trigger is "
            "keyed to buffer contents rather than to typing"
        )

        # Non-vacuity: typing the SAME text into the SAME app does open it, so
        # the assertion above is not passing because completion is broken.
        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "m", "o")
        await pilot.pause()
        assert composer.text_before_cursor() == "/mo"
        assert popup.is_open, (
            "completion never opens at all in this app — the programmatic-write "
            "assertion above was vacuous"
        )


@pytest.mark.asyncio
async def test_a_newline_in_the_composer_closes_the_menu(tmp_path) -> None:
    """Tier 2b: neither namespace is multi-line, so a newline disables
    completion — and an OPEN menu closes rather than lingering over text it no
    longer describes."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "h")
        await pilot.pause()
        assert popup.is_open

        await pilot.press("shift+enter")
        await pilot.pause()
        assert "\n" in composer.text, "test setup: shift+enter did not insert a newline"
        assert not popup.is_open, (
            "the menu survived a newline — completion is single-line only"
        )


# ── the `:` namespace end-to-end through the read-model seam ────────────────


@pytest.mark.asyncio
async def test_skill_menu_reaches_the_sessions_own_skill_list(tmp_path) -> None:
    """Tier 2b: the app resolves the ``:`` source through the
    ``ChatReadModel.completion_source`` seam and a real ``Session``'s public
    ``available_skills()`` — so what the menu offers is what that session
    actually registered, not a client-side copy that can drift."""
    session = _real_session(tmp_path, skills=_skill_entries())
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        for key in ("colon", "r", "e"):
            await pilot.press(key)
        await pilot.pause()

        assert composer.text == ":re", f"test setup: typed text is {composer.text!r}"
        assert popup.is_open, "typing :re did not open the skill menu"
        assert set(_labels(popup)) == {":review", ":refactor"}, (
            f"the skill menu did not come from the session's own list: "
            f"{popup.rendered_rows()}"
        )


@pytest.mark.asyncio
async def test_remote_client_shows_no_skill_menu(tmp_path) -> None:
    """Tier 2b: with no session behind the seam (the remote ``--connect``
    shape), typing ``:re`` shows nothing at all — not an empty menu."""
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(None))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        for key in ("colon", "r", "e"):
            await pilot.press(key)
        await pilot.pause()

        assert composer.text == ":re", "test setup: the keystrokes did not land"
        assert not popup.is_open, "a session-less client rendered a skill menu"
        assert popup.rendered_rows() == [], (
            f"a session-less client rendered rows: {popup.rendered_rows()}"
        )


# ── discoverability ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_pane_renders_the_completion_keys_in_both_states(tmp_path) -> None:
    """Tier 2b: every completion key is DISCOVERABLE through the actual rendered
    Help pane (#3314's rule: a key absent from Help does not exist for a user),
    and the state-dependent ``↑``/``↓`` is described in a way that names the
    state — otherwise a user cannot predict where the arrow goes.

    Checks the RENDERED widget, not just the ``COMPOSER_KEYS`` constant: an
    existence check on the constant would pass even if the pane never drew it
    (the #3314 co-vet finding)."""
    from textual.widgets import Static

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        rendered = str(app.query_one("#help", Static).render())

    for key, desc in COMPOSER_KEYS:
        assert desc in rendered, (
            f"({key!r}, {desc!r}) from COMPOSER_KEYS is missing from the "
            f"RENDERED Help pane"
        )

    descriptions = [desc for _key, desc in COMPOSER_KEYS]
    for affordance in (
        "open completion",
        "move completion selection",
        "accept completion",
        "dismiss completion",
    ):
        assert any(affordance in desc for desc in descriptions), (
            f"{affordance!r} is not registered in COMPOSER_KEYS, so it is "
            f"undiscoverable; have {descriptions}"
        )

    # ``↑`` is described in BOTH states — the behaviour that matters is that a
    # user can look up either meaning, not how many rows carry the key.
    arrow_descs = [desc for key, desc in COMPOSER_KEYS if "↑" in key]
    assert any("completing" in desc for desc in arrow_descs), (
        f"no ↑ row names the completing state, so a user cannot discover that "
        f"↑ moves the menu highlight: {arrow_descs}"
    )
    assert any(
        "intervention" in desc or "sent queue" in desc for desc in arrow_descs
    ), (
        f"no ↑ row names the region routing, so the menu-closed meaning became "
        f"undiscoverable: {arrow_descs}"
    )


def test_the_registry_is_the_only_source_of_command_candidates():
    """Tier 2b: the command menu enumerates the REGISTRY, never a curated
    subset — so a newly-registered slash command is completable with no change
    here (the same derive-from-registry completeness rule the drawer's
    enumerating panes follow)."""
    state = compute_completion("")
    assert state.kind == KIND_NONE, "an empty composer must not open a menu"

    state = compute_completion("/")
    offered = {c.value for c in state.candidates}
    expected = {c.name for c in REGISTRY.all_commands() if not c.hidden}
    assert offered == expected, (
        f"the command menu is not the registry's non-hidden set; "
        f"missing={expected - offered} extra={offered - expected}"
    )


# ── #3364: the argument stage's usage header ────────────────────────────────


@pytest.mark.asyncio
async def test_the_argument_stage_renders_the_commands_own_usage_line(
    tmp_path,
) -> None:
    """Tier 2b: typing ``/copy `` renders that command's REGISTERED ``usage`` as
    the popup's first row (#3364).

    Witnessed through the real widget — ``rendered_rows()`` reads the MOUNTED
    options, so this cannot pass by calling a formatter the popup never uses.
    The expected text is read from the registry rather than written out here: a
    literal would keep passing if the header started rendering some other
    command's line."""
    expected = REGISTRY.get("copy").usage
    assert expected, "test setup: /copy must declare a usage line"

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "c", "o", "p", "y", "space")
        await pilot.pause()

        assert composer.text == "/copy ", f"test setup: typed {composer.text!r}"
        rows = popup.rendered_rows()
        assert rows, (
            "the argument stage rendered nothing for a command that documents "
            "its own syntax — SlashCommand.usage is still unread"
        )
        assert rows[0].startswith(USAGE_ROW_PREFIX) and expected in rows[0], (
            f"the first row is not this command's usage line: {rows}"
        )


@pytest.mark.asyncio
async def test_a_command_without_usage_renders_no_row_at_all(tmp_path) -> None:
    """Tier 2b: ``/cost `` — a command with neither a ``usage`` line nor a
    completer — stays completely silent: no blank row, no placeholder, no
    no-match row, no crash.

    A layout that reserves a line for an absent field is worse than no feature,
    and ``NO_MATCH_ROW`` would be a lie here: nothing was ever asked for
    candidates. The ``/copy `` half at the end is the non-vacuity witness — the
    same app DOES render a header when there is one, so the silence above is not
    the feature being dead."""
    assert REGISTRY.get("cost").usage == "", (
        "test setup: /cost must be one of the commands with no usage line"
    )

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "c", "o", "s", "t", "space")
        await pilot.pause()

        assert composer.text == "/cost ", f"test setup: typed {composer.text!r}"
        assert not popup.is_open, "a command with no usage opened a menu anyway"
        assert popup.rendered_rows() == [], (
            f"a usage-less command rendered rows: {popup.rendered_rows()}"
        )

        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "c", "o", "p", "y", "space")
        await pilot.pause()
        assert popup.rendered_rows(), (
            "the header never renders in this app at all — the silence asserted "
            "above was vacuous"
        )


@pytest.mark.asyncio
async def test_the_usage_header_never_becomes_the_accepted_candidate(
    tmp_path,
) -> None:
    """Tier 2b: with a header AND candidates (``/model `` has both), the header
    occupies row 0 while the SELECTION still starts on the first real candidate,
    and ``Tab`` inserts that candidate — not the one below it.

    This is the header's sharpest structural hazard: the display gained a row
    the candidate tuple did not, so every index that crosses that boundary is a
    potential off-by-one. An accept that silently inserts the WRONG model class
    is invisible in a "the header renders" assertion. ``↑`` at the top is checked
    too — it must clamp at the first candidate rather than park on a row ``Tab``
    cannot accept."""
    session = _real_session(tmp_path)
    classes = list(session.known_model_classes())
    try:
        first_class, second_class = classes[0], classes[1]
    except IndexError:  # pragma: no cover — test setup
        pytest.fail(
            "test setup: this session configures fewer than 2 model classes, so "
            "the ↓-then-accept half below would pass vacuously"
        )
    assert REGISTRY.get("model").usage, "test setup: /model must declare a usage"

    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("slash", "m", "o", "d", "e", "l", "space")
        await pilot.pause()

        rows = popup.rendered_rows()
        assert rows[0].startswith(USAGE_ROW_PREFIX), (
            f"the header is not the first row: {rows}"
        )
        assert any(first_class in row for row in rows[1:]), (
            f"the candidates did not survive alongside the header: {rows}"
        )

        selected = popup.selected()
        assert selected is not None and selected.value == first_class, (
            f"the initial selection is not the first candidate: {selected} "
            f"(the header shifted the index)"
        )

        await pilot.press("up")
        await pilot.pause()
        assert popup.selected() == selected, (
            "↑ at the top of the list moved off the first candidate — the "
            "highlight escaped onto the header row"
        )

        await pilot.press("down")
        await pilot.pause()
        moved = popup.selected()
        assert moved is not None and moved.value == second_class, (
            f"↓ did not reach the second candidate: {moved}"
        )

        await pilot.press("up")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert composer.text == f"/model {first_class}", (
            f"Tab inserted the wrong candidate; composer holds {composer.text!r} "
            f"(expected the FIRST class, {first_class!r})"
        )


@pytest.mark.asyncio
async def test_a_usage_only_hint_does_not_claim_the_navigation_keys(
    tmp_path,
) -> None:
    """Tier 2b: a popup showing ONLY a usage header takes no keys — ``↑`` still
    reaches the sent-queue and ``Tab`` still cycles focus to the MenuBar
    (#3277), for the whole time the user is typing that command's arguments.

    15 of the 25 non-hidden commands document a syntax and offer no completer,
    so this state is not an edge case: it is what ``/visibility ``, ``/hook ``,
    ``/session `` and a dozen others now look like. Claiming ``↑``/``Tab`` there
    would eat both keys with nothing to show for it — the #3327 deadlock class.

    The ``/model `` half is the other side of the two-state witness: a popup with
    real candidates DOES claim ``Tab`` in the same app, so the assertions above
    are about the hint state, not about completion being inert."""
    from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue

    session = _real_session(tmp_path)
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_user_submitted(msg_id="m1", text="q", seq=1))
        await pilot.pause()

        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        sent_queue = app.query_one(SentQueue)
        menubar = app.query_one(MenuBar)
        assert sent_queue.has_items(), "test setup: sent-queue must be non-empty"

        async def _open_usage_hint() -> None:
            composer.clear_and_reset()
            composer.focus()
            await pilot.pause()
            await pilot.press("slash", "c", "o", "p", "y", "space")
            await pilot.pause()
            assert popup.is_open and popup.rendered_rows(), (
                "test setup: the usage hint is not showing"
            )
            assert popup.selected() is None, (
                "a hint row is offering itself as an acceptable candidate"
            )

        await _open_usage_hint()
        await pilot.press("up")
        await pilot.pause()
        assert sent_queue.has_focus, (
            "↑ was swallowed by a usage hint that has nothing to navigate — the "
            "sent-queue became unreachable while typing a command's arguments"
        )

        await _open_usage_hint()
        await pilot.press("tab")
        await pilot.pause()
        assert menubar.has_focus, (
            "Tab was swallowed by a usage hint — #3277's composer→MenuBar "
            "cycling is gone for every command that documents a syntax"
        )
        assert composer.text == "/copy ", (
            f"the hint row was inserted into the composer: {composer.text!r}"
        )

        # Non-vacuity: a popup with real candidates still owns Tab.
        composer.clear_and_reset()
        composer.focus()
        await pilot.pause()
        await pilot.press("slash", "m", "o", "d", "e", "l", "space")
        await pilot.pause()
        assert popup.selected() is not None, "test setup: /model offered nothing"
        await pilot.press("tab")
        await pilot.pause()
        assert composer.text.startswith("/model ") and composer.text != "/model ", (
            f"Tab no longer accepts a real candidate either: {composer.text!r}"
        )


# ── #3545/#3551: a long row stays ONE candidate, one line ──────────────────


def _wrapping_skill_entries():
    """Real ``SkillEntry`` dataclasses whose descriptions are long enough to
    WRAP at the harness's terminal width — the condition #3545 was reported
    under, and the one the short ``/`` summaries never reach."""
    return [
        SkillEntry(
            name="draft_review",
            description=(
                "Draft an artifact, self-review it against your own checklist "
                "via a schema-validated agent step, and revise on failure"
            ),
            path="a.md",
        ),
        SkillEntry(
            name="draft_publish",
            description=(
                "Publish a reviewed artifact to its destination and record the "
                "hand-off in the audit trail for later reconstruction"
            ),
            path="b.md",
        ),
        SkillEntry(
            name="draft_translate",
            # Wide characters: two cells each, so a clip measured in CHARACTERS
            # would leave this row overflowing by its own width — and Textual
            # re-wraps an overflowing row at column 0, which is #3545's symptom
            # arriving through a different door.
            description=(
                "生成物を対象言語へ翻訳し、用語集との整合を検査したうえで、"
                "監査証跡に翻訳元と訳文の対応を記録します"
            ),
            path="c.md",
        ),
    ]


@pytest.mark.asyncio
async def test_a_long_skill_row_is_clipped_to_one_line(tmp_path) -> None:
    """Tier 2b: a skill description too long for the width occupies ONE line and
    says it was cut (#3551, owner ruling A — replaces the wrap this test used to
    pin).

    The row is read off ``rendered_rows`` (the mounted options) rather than off
    ``CompletionState``, so this is a claim about the widget rather than about a
    formatter it might not call.

    Three things, and the third is the one a lazy implementation fails: one
    visual line, an ellipsis saying text was dropped, and the description's
    BEGINNING still present — clipping to the width must not cost the reader the
    part that distinguishes two skills.
    """
    entries = _wrapping_skill_entries()
    session = _real_session(tmp_path, skills=entries)
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("colon")
        await pilot.pause()

        assert popup.is_open, "typing : did not open the skill menu"
        rows = popup.rendered_rows()
        # Precondition, asserted rather than assumed, and against the width the
        # widget actually has: a description that already FIT would satisfy
        # every check below while testing nothing about clipping.
        width = popup.scrollable_content_region.width
        assert any(
            cell_len(f":{e.name}  {e.description}") > width for e in entries
        ), f"test setup: every row already fits {width} cells, nothing to clip"
        for entry in entries:
            row = next((r for r in rows if r.startswith(f":{entry.name}")), None)
            assert row is not None, f"{entry.name} is missing from the menu: {rows}"
            assert "\n" not in row, f"the row is more than one line: {row!r}"
            assert row.endswith(ROW_ELLIPSIS), (
                f"text was dropped without saying so: {row!r}"
            )
            head = entry.description[:20]
            assert head in row, (
                f"the clip ate the start of the description, which is what "
                f"tells two skills apart — mounted {row!r}, expected to open "
                f"with {head!r}"
            )


@pytest.mark.asyncio
async def test_no_row_overflows_the_width_in_CELLS(tmp_path) -> None:
    """Tier 2b: #3545's invariant, carried through #3551's change of mechanism —
    a candidate must never read as two.

    #3545's symptom was three skill rows reading as six, because Textual returns
    each continuation to column 0, exactly where the NEXT candidate's ``:name``
    starts. #3551 replaced the hanging indent with a one-line clip, which
    removes continuations — but only while every row FITS. A row one cell too
    wide is re-wrapped by Textual itself and the symptom returns unchanged, so
    the invariant to hold is the width, not the absence of a newline in reyn's
    own output.

    Measured in cells, against a fixture that includes wide characters: a clip
    written with ``len`` passes a character-counting assertion and still
    overflows the terminal.
    """
    entries = _wrapping_skill_entries()
    session = _real_session(tmp_path, skills=entries)
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("colon")
        await pilot.pause()

        assert popup.is_open, "typing : did not open the skill menu"
        width = popup.scrollable_content_region.width
        assert width > 0, "test setup: the popup was never laid out"
        rows = popup.rendered_rows()
        assert rows, "test setup: the menu mounted no rows"
        wide = [r for r in rows if r.startswith(":draft_translate")]
        assert wide, f"test setup: the wide-character row is missing: {rows}"
        for row in rows:
            for line in row.split("\n"):
                assert cell_len(line) <= width, (
                    f"a row is {cell_len(line)} cells wide in a {width}-cell "
                    f"region — Textual will re-wrap it at column 0 and it will "
                    f"read as a second candidate: {line!r}"
                )


@pytest.mark.asyncio
async def test_one_wrapped_candidate_stays_one_selectable_option(tmp_path) -> None:
    """Tier 2b: wrapping a row must not split it into several OPTIONS — ``↓``
    from the first candidate lands on the second candidate, not on the first
    one's second line (#3545).

    The cheap way to indent a continuation is to mount it as its own row, and
    every visual assertion above would pass under it while ``↓``, the highlight
    and the Tab accept all silently walked half a description.
    """
    entries = _wrapping_skill_entries()
    session = _real_session(tmp_path, skills=entries)
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("colon")
        await pilot.pause()

        candidates = popup.state().candidates
        assert {c.value for c in candidates} == {e.name for e in entries}, (
            f"test setup: ↓ needs both wrapping candidates on offer: "
            f"{candidates}"
        )

        def _highlighted_row() -> str:
            return popup.rendered_rows()[popup.highlighted]

        assert popup.selected() is candidates[0], (
            f"test setup: the menu did not open on its first candidate: "
            f"{popup.selected()}"
        )
        await pilot.press("down")
        await pilot.pause()
        assert popup.selected() is candidates[1], (
            f"↓ from a wrapped candidate did not reach the NEXT candidate: "
            f"{popup.selected()}"
        )
        # The row the user SEES highlighted has to be the candidate Tab would
        # accept. Mounting each visual line as its own option keeps the two
        # assertions above passing — ``selected()`` indexes the candidate
        # tuple, not the option list — while the highlight sits on some other
        # candidate's continuation line.
        assert _highlighted_row().startswith(popup.selected().label), (
            f"the highlighted row is not the candidate that would be accepted "
            f"— highlighted {_highlighted_row()!r}, accept target "
            f"{popup.selected().label!r}"
        )


@pytest.mark.asyncio
async def test_an_open_menu_re_clips_when_the_terminal_width_changes(tmp_path) -> None:
    """Tier 2b: a menu that is already open re-clips for the new width when the
    terminal is resized under it (#3545, #3551).

    The clip is computed against a width, so it goes stale the moment the width
    moves; a menu is open exactly while the user is typing, which is also when
    nothing else would rebuild it. A stale clip is worse under #3551 than the
    stale wrap it replaces: too-wide rows are re-wrapped by Textual at column 0,
    so every candidate reads as two.

    Asserted on the invariant rather than on the resulting text: whatever the
    width, every row still fits it in cells and still opens with its own name.
    """
    entries = _wrapping_skill_entries()
    session = _real_session(tmp_path, skills=entries)
    transport = RecordingTransport()
    app = TextualChatApp(transport=transport, read_model=SessionReadModel(session))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("colon")
        await pilot.pause()
        assert popup.is_open, "typing : did not open the skill menu"
        before = popup.rendered_rows()

        await pilot.resize_terminal(70, 30)
        await pilot.pause()

        after = popup.rendered_rows()
        assert after != before, (
            f"the menu kept the clip it was built with at the old width: "
            f"{after}"
        )
        width = popup.scrollable_content_region.width
        for entry in entries:
            row = next((r for r in after if r.startswith(f":{entry.name}")), None)
            assert row is not None, f"{entry.name} vanished on resize: {after}"
            assert cell_len(row) <= width, (
                f"the re-clip left a {cell_len(row)}-cell row in a {width}-cell "
                f"region: {row!r}"
            )
