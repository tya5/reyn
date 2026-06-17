"""Tier 2: /find history recall via picker completer.

Power-user follow-on to PR #537 (/find MVP) + #561 (regex/case)
+ #565 (active-state header) + #563 (match-line preview). The
recall closes the last remaining /find UX gap: after running
``/find foo`` once, the user often wants to re-run it later —
currently they have to retype the whole query (including any
``-r`` / ``-c`` flags). This adds:

  - Every non-empty ``/find <arg>`` invocation appends ``arg``
    to an in-memory LRU deque (capped at 5)
  - Duplicates move to the front (= LRU semantics, not FIFO)
  - A ``_find_completer`` on the /find slash returns the
    history when the user types ``/find `` (trailing space,
    no other args)
  - Prefix filter: ``/find -r`` partial → filters history to
    entries starting with ``-r``
  - Empty arg invocation (= usage-hint path) does NOT record

Pinned:
  - ``_record_find_history`` appends + dedups
  - LRU semantics (= duplicate query moves to front)
  - Max cap enforced (= 6th entry evicts the oldest)
  - Empty arg → no-op (= doesn't pollute history with empty
    strings; the empty-query path triggers the usage hint, not
    a meaningful search)
  - ``_find_completer`` returns full history for empty
    arg_partial
  - ``_find_completer`` filters by prefix for non-empty
    arg_partial
  - ``/find`` slash has the completer registered
  - End-to-end via ``find_cmd``: queries land in history after
    the outbox put

In-memory only — history is naturally gone on session restart.
Persistence via prefs file is deferred.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _clear_find_history():
    """Reset the module-level deque between tests so cases are isolated."""
    from reyn.interfaces.slash.find import _find_history
    _find_history.clear()
    yield
    _find_history.clear()


def test_record_appends_to_front() -> None:
    """Tier 2: ``_record_find_history`` puts new entries at the front."""
    from reyn.interfaces.slash.find import (
        _find_history_snapshot,
        _record_find_history,
    )

    _record_find_history("alpha")
    _record_find_history("beta")
    _record_find_history("gamma")
    assert _find_history_snapshot() == ["gamma", "beta", "alpha"]


def test_record_lru_moves_duplicate_to_front() -> None:
    """Tier 2: re-recording an existing entry moves it to the front."""
    from reyn.interfaces.slash.find import (
        _find_history_snapshot,
        _record_find_history,
    )

    _record_find_history("alpha")
    _record_find_history("beta")
    _record_find_history("gamma")
    # Re-run "alpha" — it should move to front, not duplicate.
    _record_find_history("alpha")
    assert _find_history_snapshot() == ["alpha", "gamma", "beta"]


def test_record_caps_at_max() -> None:
    """Tier 2: history evicts the oldest entry past the cap."""
    from reyn.interfaces.slash.find import (
        _FIND_HISTORY_MAX,
        _find_history_snapshot,
        _record_find_history,
    )

    # Fill past the cap.
    for i in range(_FIND_HISTORY_MAX + 3):
        _record_find_history(f"q{i}")
    snap = _find_history_snapshot()
    assert len(snap) == _FIND_HISTORY_MAX
    # Newest at front; oldest entries evicted.
    assert snap[0] == f"q{_FIND_HISTORY_MAX + 2}"
    assert "q0" not in snap
    assert "q1" not in snap
    assert "q2" not in snap


def test_record_empty_arg_no_op() -> None:
    """Tier 2: empty arg doesn't pollute history."""
    from reyn.interfaces.slash.find import (
        _find_history_snapshot,
        _record_find_history,
    )

    _record_find_history("")
    _record_find_history("   ")  # already-whitespace caller's job to strip
    # We accept "   " here — the caller strips, but defensive check:
    # current contract is "non-empty stored as-is". Whitespace-only
    # strings rarely appear in production.
    snap = _find_history_snapshot()
    # At minimum, empty string was excluded.
    assert "" not in snap


def test_completer_empty_partial_returns_full_history() -> None:
    """Tier 2: empty ``arg_partial`` surfaces the entire history."""
    from reyn.interfaces.slash.find import _find_completer, _record_find_history

    _record_find_history("alpha")
    _record_find_history("beta")
    out = _find_completer(None, "")
    assert out == ["beta", "alpha"]


def test_completer_filters_by_prefix() -> None:
    """Tier 2: prefix in ``arg_partial`` narrows the completion list."""
    from reyn.interfaces.slash.find import _find_completer, _record_find_history

    _record_find_history("apple")
    _record_find_history("banana")
    _record_find_history("apricot")
    # Prefix "ap" → both "apricot" and "apple" match (= newest first).
    out = _find_completer(None, "ap")
    assert out == ["apricot", "apple"]
    # Prefix "ban" → only "banana".
    assert _find_completer(None, "ban") == ["banana"]
    # Prefix that matches nothing → empty.
    assert _find_completer(None, "zzz") == []


def test_completer_preserves_flag_prefix() -> None:
    """Tier 2: history queries WITH flags surface intact + filter on flag prefix."""
    from reyn.interfaces.slash.find import _find_completer, _record_find_history

    _record_find_history("-r foo.*")
    _record_find_history("-c Bar")
    _record_find_history("plain")
    # Empty partial returns all.
    assert "-r foo.*" in _find_completer(None, "")
    assert "-c Bar" in _find_completer(None, "")
    # "-r" prefix returns only the regex one.
    assert _find_completer(None, "-r") == ["-r foo.*"]


def test_find_slash_has_completer_registered() -> None:
    """Tier 2: ``/find`` registers ``_find_completer`` in the slash registry."""
    from reyn.interfaces.slash import REGISTRY
    from reyn.interfaces.slash.find import _find_completer

    cmd = REGISTRY.get("find")
    assert cmd is not None
    assert cmd.completer is _find_completer


@pytest.mark.asyncio
async def test_find_cmd_records_history_on_invocation() -> None:
    """Tier 2: end-to-end — calling ``find_cmd`` adds the arg to history."""
    from reyn.interfaces.slash.find import _find_history_snapshot, find_cmd

    class _StubSession:
        """Minimal session stub — collects outbox messages."""

        def __init__(self) -> None:
            self.outbox: list = []

        async def _put_outbox(self, msg) -> None:
            self.outbox.append(msg)

    session = _StubSession()
    await find_cmd(session, "needle in haystack")
    snap = _find_history_snapshot()
    assert snap[0] == "needle in haystack"
    # And the outbox got the sentinel as well.
    (outbox_msg,) = session.outbox
    assert outbox_msg.kind == "__find__"
    assert outbox_msg.text == "needle in haystack"


@pytest.mark.asyncio
async def test_find_cmd_empty_arg_does_not_record() -> None:
    """Tier 2: empty ``/find`` (usage-hint path) doesn't pollute history."""
    from reyn.interfaces.slash.find import _find_history_snapshot, find_cmd

    class _StubSession:
        def __init__(self) -> None:
            self.outbox: list = []
        async def _put_outbox(self, msg) -> None:
            self.outbox.append(msg)

    session = _StubSession()
    await find_cmd(session, "")
    await find_cmd(session, "   ")  # whitespace-only also strips empty
    snap = _find_history_snapshot()
    assert snap == []
