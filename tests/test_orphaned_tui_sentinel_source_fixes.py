"""Tier 2: orphaned TUI-only outbox sentinels are removed at source.

Four slash-command / handler emit sites that previously emitted kinds with
no live consumer in the inline CUI (``__quit__``, ``__donut__``,
``__matrix__``, ``intervention_resolved``) have been fixed.  These tests
are *falsify-flip*: each assertion was RED before the fix and GREEN after.
Flipping an assertion back to the old expected value would make it fail
again, proving the sentinel has been removed from the outbox path.

Consumer analysis (why zero-consumer):
- ``__quit__`` was consumed by ``app_outbox._on_quit`` (Textual TUI, deleted).
  The inline CUI intercepts ``/quit`` / ``/exit`` at ``_accept()`` before
  ``submit_user_text`` — the handler is dead code in normal paths.
- ``__donut__`` / ``__matrix__`` were consumed by Textual modal screens
  (deleted).  Unknown kinds fall through ``format_inline_message`` to
  ``Text(msg.text)`` — empty text prints two blank lines; ``__donut__``
  produced a blank-line artefact.
- ``intervention_resolved`` was consumed by a Textual widget callback
  (deleted).  The inline CUI detects resolution via ``_sync_region()``
  poll on ``session.interventions.head()`` — no signal needed.

``intervention_resolved`` is covered in
``test_intervention_handler_invariants.py``; the remaining three are
covered here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


def _drain_outbox(session: Session) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quit_handler_emits_no_quit_sentinel(tmp_path, monkeypatch) -> None:
    """Tier 2: /quit handler is a no-op — ``__quit__`` never reaches the outbox.

    The inline CUI intercepts ``/quit`` / ``/exit`` at ``_accept()`` before
    ``submit_user_text``.  The handler was dead code; it now emits nothing.
    Falsify-flip: asserting ``__quit__`` IS in outbox_kinds would fail with
    the current fix, proving the sentinel has been removed at source.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/quit")
    assert consumed is True

    outbox_kinds = [m.kind for m in _drain_outbox(session)]
    assert "__quit__" not in outbox_kinds, (
        "__quit__ must NOT reach the outbox — no live consumer; "
        "the inline CUI intercepts quit at the input level"
    )


@pytest.mark.asyncio
async def test_exit_handler_emits_no_quit_sentinel(tmp_path, monkeypatch) -> None:
    """Tier 2: /exit alias is also a no-op — ``__quit__`` never reaches outbox.

    Both ``/quit`` and ``/exit`` share the same no-op handler.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/exit")
    assert consumed is True

    outbox_kinds = [m.kind for m in _drain_outbox(session)]
    assert "__quit__" not in outbox_kinds, (
        "__quit__ must NOT reach the outbox via /exit either"
    )


@pytest.mark.asyncio
async def test_donut_emits_system_reply_not_tui_sentinel(tmp_path, monkeypatch) -> None:
    """Tier 2: /donut emits a ``system`` reply — ``__donut__`` never reaches outbox.

    The Textual donut modal consumer is deleted.  The easter egg is preserved
    as an inline ``reply()`` (kind=``system``).  Falsify-flip: asserting
    ``__donut__`` IS in outbox_kinds would fail, proving the sentinel is gone.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/donut")
    assert consumed is True

    messages = _drain_outbox(session)
    outbox_kinds = [m.kind for m in messages]
    assert "__donut__" not in outbox_kinds, (
        "__donut__ must NOT reach the outbox — no live consumer"
    )
    assert "system" in outbox_kinds, (
        "/donut must emit a system-kind reply so the easter egg is visible inline"
    )


@pytest.mark.asyncio
async def test_matrix_emits_system_reply_not_tui_sentinel(tmp_path, monkeypatch) -> None:
    """Tier 2: /matrix emits a ``system`` reply — ``__matrix__`` never reaches outbox.

    The Textual matrix-rain modal consumer is deleted.  The easter egg text
    ("There is no spoon.") is preserved as an inline ``reply()`` (kind=``system``).
    Falsify-flip: asserting ``__matrix__`` IS in outbox_kinds would fail.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/matrix")
    assert consumed is True

    messages = _drain_outbox(session)
    outbox_kinds = [m.kind for m in messages]
    assert "__matrix__" not in outbox_kinds, (
        "__matrix__ must NOT reach the outbox — no live consumer"
    )
    assert "system" in outbox_kinds, (
        "/matrix must emit a system-kind reply so the easter egg is visible inline"
    )
    assert any(m.text == "There is no spoon." for m in messages), (
        "/matrix reply must preserve the easter egg text"
    )
