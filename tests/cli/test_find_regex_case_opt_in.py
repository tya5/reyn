"""Tier 2: /find regex + case-sensitive opt-in flags.

Follow-on to PR #537 (/find MVP) + #539 (Ctrl+G cycle) + #542
(discoverability) + #552 (usage field). The MVP was hard-wired
to case-insensitive substring; this PR adds opt-in flags for
power users:

  /find foo            → case-insensitive substring (default; unchanged)
  /find -c Foo         → case-sensitive substring
  /find -r f.*o        → case-insensitive regex
  /find -rc Foo.*      → case-sensitive regex (combined flag)

Pinned:
  - ``ConversationView.find_in_buffer`` accepts ``regex`` and
    ``case_sensitive`` kwargs; default = both False (= MVP path)
  - Invalid regex raises ``re.error`` so the caller can surface
    a clear status
  - Flag parser splits ``"-rc Foo.*"`` into (regex=True, case=True,
    query="Foo.*") with order-independence (``-cr`` = ``-rc``)
  - Unrecognised flag combos fall through as literal query
    (= ``/find -hyphen-term`` searches for ``-hyphen-term``)
  - Cycle state remembers the flags so Ctrl+G keeps applying the
    same mode as the buffer mutates between presses
  - Invalid regex in /find → error status, no crash
  - /find summary + usage updated for the new flag set
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.text import Text

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _seed(conv, lines: list[str]) -> None:
    log = conv._log()
    for line in lines:
        log.write(Text(line))


# ── _parse_find_flags ────────────────────────────────────────────────────────


def test_parse_no_flag_is_default_path() -> None:
    """Tier 2: bare query → both flags False, full arg preserved."""
    from reyn.interfaces.tui.app_outbox import _parse_find_flags

    assert _parse_find_flags("foo") == (False, False, "foo")
    assert _parse_find_flags("foo bar baz") == (False, False, "foo bar baz")
    assert _parse_find_flags("") == (False, False, "")


def test_parse_recognises_individual_flags() -> None:
    """Tier 2: ``-c`` / ``-r`` parsed cleanly."""
    from reyn.interfaces.tui.app_outbox import _parse_find_flags

    assert _parse_find_flags("-c Foo") == (False, True, "Foo")
    assert _parse_find_flags("-r f.*o") == (True, False, "f.*o")


def test_parse_combined_flag_order_independent() -> None:
    """Tier 2: ``-rc`` and ``-cr`` mean the same."""
    from reyn.interfaces.tui.app_outbox import _parse_find_flags

    assert _parse_find_flags("-rc Foo.*") == (True, True, "Foo.*")
    assert _parse_find_flags("-cr Foo.*") == (True, True, "Foo.*")


def test_parse_unrecognised_flag_treated_as_query() -> None:
    """Tier 2: ``-foo`` is not a flag, falls through as a literal query.

    The user might be searching for a hyphen-prefixed term in
    their conv (e.g., ``-h`` or ``--force``). Without this fall-
    through they'd have no way to /find it.
    """
    from reyn.interfaces.tui.app_outbox import _parse_find_flags

    assert _parse_find_flags("-foo bar") == (False, False, "-foo bar")
    assert _parse_find_flags("-x") == (False, False, "-x")


def test_parse_flag_only_no_query_returns_empty_query() -> None:
    """Tier 2: ``-r`` alone (no query) → flag set, empty query.

    The caller surfaces a usage hint when query is empty, so the
    parser just splits cleanly without inventing content.
    """
    from reyn.interfaces.tui.app_outbox import _parse_find_flags

    assert _parse_find_flags("-r") == (True, False, "")
    assert _parse_find_flags("-c") == (False, True, "")


# ── find_in_buffer flag-aware ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_in_buffer_case_sensitive_excludes_lowercase_variants() -> None:
    """Tier 2: case-sensitive substring matches case exactly."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        _seed(conv, ["Foo line", "foo line", "FOO line"])
        await pilot.pause()
        matches = conv.find_in_buffer("Foo", case_sensitive=True)
        texts = [m[1] for m in matches]
        assert "Foo line" in texts
        assert "foo line" not in texts
        assert "FOO line" not in texts


@pytest.mark.asyncio
async def test_find_in_buffer_regex_pattern_matches() -> None:
    """Tier 2: regex search picks up pattern-matching lines."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        _seed(conv, ["foo123", "fox456", "bar789", "fooXXX"])
        await pilot.pause()
        # Pattern: 'fo' then any chars then 'X'.
        matches = conv.find_in_buffer(r"fo.*X", regex=True)
        texts = [m[1] for m in matches]
        assert "fooXXX" in texts
        # Plain substring "fo.*X" doesn't appear literally → only
        # regex-mode matches return anything.
        assert "foo123" not in texts


@pytest.mark.asyncio
async def test_find_in_buffer_regex_case_sensitive() -> None:
    """Tier 2: regex + case-sensitive only matches exact case patterns."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        _seed(conv, ["Foo1", "foo1", "Foo2"])
        await pilot.pause()
        matches = conv.find_in_buffer(
            r"Foo\d", regex=True, case_sensitive=True,
        )
        texts = [m[1] for m in matches]
        assert "Foo1" in texts
        assert "Foo2" in texts
        assert "foo1" not in texts


@pytest.mark.asyncio
async def test_find_in_buffer_invalid_regex_raises() -> None:
    """Tier 2: invalid regex bubbles up so the caller can surface it."""
    import re

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        with pytest.raises(re.error):
            conv.find_in_buffer("foo(", regex=True)


# ── _on_find dispatch end-to-end ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_find_with_flags_seeds_router_state() -> None:
    """Tier 2: /find -r preserves the regex flag in router cycle state."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed(conv, ["abc", "abx", "xyz"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="-r ab."),
            conv,
            header,
        )
        await pilot.pause()
        assert router.find_query == "ab."
        assert router.find_regex_enabled is True
        assert router.find_case_sensitive is False
        assert router.find_cursor_index is not None


@pytest.mark.asyncio
async def test_on_find_invalid_regex_emits_error_status() -> None:
    """Tier 2: malformed regex surfaces an error status, clears cycle state."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed(conv, ["sample"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="-r foo("),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "invalid pattern" in snap["body"]
        # No stale cycle state.
        assert router.find_query is None


@pytest.mark.asyncio
async def test_cycle_find_preserves_flags() -> None:
    """Tier 2: Ctrl+G re-search uses the same flags as the initial /find."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        # Mixed-case content. Case-sensitive search should walk
        # only the Capitalised entries.
        _seed(conv, ["Foo1", "foo2", "Foo3", "fox4"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="-c Foo"),
            conv,
            header,
        )
        await pilot.pause()
        # Cycle forward — should walk only the Foo1/Foo3 case-
        # sensitive matches (skipping foo2).
        prev_cursor = router.find_cursor_index
        router.cycle_find(+1)
        await pilot.pause()
        # The cursor moved AND case_sensitive flag survived.
        assert router.find_case_sensitive is True
        new_cursor = router.find_cursor_index
        # New target is the OTHER Foo line, not the foo2 lower-case line.
        assert new_cursor != prev_cursor
        # Sanity: foo2 is at idx between Foo1 and Foo3; verify cursor
        # avoided it by inspecting the match list directly.
        matches = conv.find_in_buffer("Foo", case_sensitive=True)
        match_idxs = [m[0] for m in matches]
        assert new_cursor in match_idxs


@pytest.mark.asyncio
async def test_flag_only_no_query_shows_usage_hint() -> None:
    """Tier 2: ``/find -r`` with no query falls through to the usage hint."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="-r"),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "usage:" in snap["body"]


def test_find_slash_summary_and_usage_updated() -> None:
    """Tier 2: /find's structured usage field reflects the new flag set."""
    from reyn.interfaces.slash import REGISTRY

    cmd = REGISTRY.get("find")
    assert cmd is not None
    assert cmd.usage == "/find [-r|-c|-rc] <query>"
    # Summary mentions both modes.
    assert "substring" in cmd.summary.lower()
    assert "regex" in cmd.summary.lower()
