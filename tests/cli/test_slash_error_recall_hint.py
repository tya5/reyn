"""Tier 2: slash error → sticky recall hint + InputBar history capture (W13 T2-3).

Wave-13 finding B#3: when a slash command fails (e.g. ``/image`` without a
path, ``/attach`` unknown agent), the InputBar is cleared and the slash picker
is closed.  The user must retype from scratch — there is no re-edit path.

Fix:

1. ``_maybe_handle_slash`` detects when the handler emitted a
   ``kind="error"`` outbox item and appends a ``kind="status"`` message
   with ``meta.source="slash_recall_hint"`` so the TUI sticky bar shows a
   3 s hint ``↑ to recall `/<cmd>```.

2. ``OutboxRouter._on_status`` routes ``source="slash_recall_hint"``
   through ``_show_transient_status`` (3 s auto-hide, ``kind="general"``)
   instead of the persistent thinking indicator path.

3. ``InputBar._submit`` already captures every submit (error or success)
   into ``_history`` — no change required; tests confirm the invariant.

Pinned (per spec):

1. ``/image`` (no path) → error fired → outbox contains recall hint with
   "↑ to recall" and "/image"; sticky snapshot shows it.
2. ``/reset`` (no args → plain ``reply()``) → NO recall sticky.
3. ``InputBar._submit("/image\\n")`` → history contains "/image".
4. ``InputBar._submit("/image bad path\\n")`` (which would error at session
   level) → history still contains "/image bad path" (InputBar is
   agnostic to slash outcome).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_session(tmp_path: Path, *, agent_name: str = "t") -> "ChatSession":
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog

    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _drain_outbox(session: "ChatSession") -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


# ── test 1: /image (no path) → recall hint appears in outbox + sticky ─────────


@pytest.mark.asyncio
async def test_image_no_path_emits_recall_hint_in_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: /image without a path → error → recall hint in outbox.

    Drives ``_maybe_handle_slash`` with ``/image`` (empty args) and
    verifies the session appends a ``kind="status"`` message whose text
    contains "↑ to recall" and "/image".  Uses the public ``outbox``
    queue (= no private state inspection).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/image")

    msgs = _drain_outbox(session)
    recall_msgs = [
        m for m in msgs
        if m.kind == "status"
        and (m.meta or {}).get("source") == "slash_recall_hint"
    ]
    assert recall_msgs, (
        "Expected at least one kind='status' recall hint message after /image error; "
        f"got outbox kinds: {[m.kind for m in msgs]!r}"
    )
    hint_text = recall_msgs[0].text
    assert "↑ to recall" in hint_text, (
        f"Recall hint text must contain '↑ to recall', got: {hint_text!r}"
    )
    assert "/image" in hint_text, (
        f"Recall hint text must contain '/image', got: {hint_text!r}"
    )


@pytest.mark.asyncio
async def test_image_no_path_recall_hint_visible_in_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: /image error recall hint → TUI sticky snapshot shows it.

    Synthesises the recall hint OutboxMessage (same as _maybe_handle_slash
    emits) and drives it through OutboxRouter._on_status.  Verifies via
    ``StickyStatus.snapshot()`` that the sticky shows the recall text with
    ``kind="general"`` (= transient, not the persistent thinking kind).
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        router = OutboxRouter(app)
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        msg = OutboxMessage(
            kind="status",
            text="↑ to recall `/image`",
            meta={"source": "slash_recall_hint"},
        )
        router._on_status(msg, conv, header)
        await pilot.pause()

        sticky = conv._sticky()
        assert sticky is not None, "StickyStatus must be mounted"
        snap = sticky.snapshot()
        assert snap["active"] is True, (
            f"Sticky must be active after recall hint, got active={snap['active']!r}"
        )
        assert snap["kind"] == "general", (
            f"Recall hint sticky must be kind='general' (transient), "
            f"got kind={snap['kind']!r}"
        )
        assert "↑ to recall" in snap["body"], (
            f"Sticky body must contain '↑ to recall', got body={snap['body']!r}"
        )
        assert "/image" in snap["body"], (
            f"Sticky body must contain '/image', got body={snap['body']!r}"
        )


# ── test 2: /reset (no args → plain reply) → NO recall sticky ─────────────────


@pytest.mark.asyncio
async def test_reset_success_does_not_emit_recall_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: /reset (no confirm → plain reply(), not reply_error()) → no recall.

    ``/reset`` without "confirm" prints a warning via ``reply()`` (not
    ``reply_error()``).  No ``kind="error"`` is emitted, so the recall
    hint must NOT appear in the outbox.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    await session._maybe_handle_slash("/reset")

    msgs = _drain_outbox(session)
    recall_msgs = [
        m for m in msgs
        if m.kind == "status"
        and (m.meta or {}).get("source") == "slash_recall_hint"
    ]
    assert not recall_msgs, (
        "No recall hint must appear when slash command only calls reply() "
        f"(not reply_error()); got recall msgs: {recall_msgs!r}"
    )


# ── test 3: InputBar captures slash submit in history (success path) ───────────


@pytest.mark.asyncio
async def test_inputbar_history_captures_slash_submit() -> None:
    """Tier 2: InputBar._submit captures slash command text in _history.

    Calls ``_submit`` directly with ``/image`` text.  The history must
    contain the submitted text regardless of whether the slash command
    succeeds or errors — InputBar is agnostic to slash command outcome.
    Public surface: ``_history`` deque (= the collection, not private
    state of any individual item).
    """
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        input_bar = app.query_one("#inputbar", InputBar)
        ta = app.query_one("#input", TextArea)

        ta.load_text("/image")
        input_bar._submit(ta)

        assert "/image" in input_bar._history, (
            "InputBar history must contain '/image' after _submit; "
            f"got history: {list(input_bar._history)!r}"
        )


# ── test 4: InputBar captures errored slash submit in history ─────────────────


@pytest.mark.asyncio
async def test_inputbar_history_captures_errored_slash_submit() -> None:
    """Tier 2: InputBar captures slash text even when the command will error.

    InputBar doesn't know whether a slash command errored at the session
    level.  History capture happens unconditionally in ``_submit`` before
    the ``UserSubmitted`` message is dispatched to the session.  This
    test drives ``/image bad-path`` — a command whose args would trigger
    a reply_error at the session level — and asserts the history entry
    still lands.
    """
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        input_bar = app.query_one("#inputbar", InputBar)
        ta = app.query_one("#input", TextArea)

        ta.load_text("/image bad-path")
        input_bar._submit(ta)

        assert "/image bad-path" in input_bar._history, (
            "InputBar history must contain '/image bad-path' after _submit; "
            f"got history: {list(input_bar._history)!r}"
        )
