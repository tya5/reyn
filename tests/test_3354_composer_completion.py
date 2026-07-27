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

from reyn.core.events.state_log import StateLog
from reyn.data.skills.registry import SkillEntry
from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import COMPOSER_KEYS
from reyn.interfaces.inline.textual_chat.completion import (
    KIND_ARGUMENT,
    KIND_COMMAND,
    KIND_NONE,
    KIND_SKILL,
    NO_MATCH_ROW,
    CompletionPopup,
    compute_completion,
)
from reyn.interfaces.repl.read_model import ChatReadModel
from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.schemas.models import Event
from tests._support.agent_session import make_session

# ── real collaborators ───────────────────────────────────────────────────────


class RecordingTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` (same shape as
    ``test_3327_keyboard_reachability_to_panel.RecordingTransport``): stays open
    on a live queue so a test can push a ``user_submitted`` event to populate the
    sent-queue, and records what was submitted."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.submitted: "list[str]" = []

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
        pass

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class SessionReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (same pattern as
    ``test_3338``'s ``_MutableSnapshotReadModel``) whose
    :meth:`completion_session` returns a REAL ``Session`` — the seam the app
    resolves both completion sources through."""

    def __init__(self, session=None) -> None:
        self._session = session

    def completion_session(self):
        return self._session

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

    before = compute_completion("/ima", session=session)
    assert before.kind == KIND_COMMAND
    assert "/image" in [c.label for c in before.candidates], (
        f"the command stage did not offer /image; got {[c.label for c in before.candidates]}"
    )

    after = compute_completion("/image ", session=session)
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

    state = compute_completion("/image sh", session=session)
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

    state = compute_completion("/model ", session=session)
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
        (":re", True, "colon at input start with 2+ chars"),
        ("run :re", True, "colon after whitespace with 2+ chars"),
        (":r", False, "1 char fails the length gate"),
        (":", False, "bare colon fails the length gate"),
        ("http://xx", False, "colon mid-word fails the word-boundary gate"),
        ("12:30", False, "a time fails the word-boundary gate"),
        ("ratio:20", False, "colon mid-word fails the word-boundary gate"),
        ("note: se", False, "a space after the colon is not a skill token"),
    ],
)
def test_skill_trigger_requires_word_boundary_and_minimum_length(
    text, should_trigger, why,
):
    """Tier 2b: the ``:`` trigger needs BOTH gates, not either.

    The two are independently load-bearing and this table shows each catching a
    case the other misses: ``http://xx`` passes the length gate and is caught
    only by the word boundary; ``:r`` passes the word boundary and is caught
    only by the length gate. A colon is far too common in prose for one gate."""
    state = compute_completion(text, skills=_skill_entries())
    triggered = state.kind != KIND_NONE
    assert triggered is should_trigger, (
        f"{text!r}: expected trigger={should_trigger} ({why}), got kind={state.kind!r}"
    )


# ── source availability vs. no matches (remote stays SILENT) ─────────────────


def test_unreadable_sources_stay_silent_rather_than_showing_an_empty_menu():
    """Tier 2b: a client that cannot read a namespace's source shows NOTHING —
    never an empty-looking menu, which would read as "no such command exists".

    This is the remote ``--connect`` case: no local ``Session``, so no
    ``CompleterFn`` can be called and no skill list can be enumerated."""
    assert compute_completion("/image ", session=None).kind == KIND_NONE, (
        "argument completion must be silent without a session, not an empty menu"
    )
    assert compute_completion(":re", skills=None).kind == KIND_NONE, (
        "skill completion must be silent when the skill source is unavailable"
    )
    # But COMMAND-name completion is registry-derived and works everywhere.
    remote = compute_completion("/im", session=None, skills=None)
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

        assert transport.submitted == ["/he"], (
            "Enter with the menu open must submit the TYPED text, not accept "
            f"the highlighted candidate; got {transport.submitted}"
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
    ``ChatReadModel.completion_session`` seam and a real ``Session``'s public
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
