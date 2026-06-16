"""Tier 2: StreamingRow timer lifecycle + linear accumulation invariants.

Streaming / perf UX audit surfaced two coupled HIGH findings:

  • Perf F1 — ``set_interval(_RENDER_INTERVAL_S, _flush_render)`` ran
    at 60 Hz but its handle was never stored, so the timer kept firing
    forever after ``seal()`` (and even after ``row.remove()``). A
    20-turn dogfood produced 1200 dead callbacks/s on the event loop.
  • Perf F2 — ``_build_renderable`` did ``"".join(self._chunks)`` on
    every flush, making the total render cost O(N²) over an N-chunk
    stream. For a 4 000-token reply this was ~32 MB of string copies
    before seal.

The fix:
  1. Cache the interval handle in ``_interval_handle`` and ``stop()`` it
     from ``seal()`` (and from the deferred-mount path).
  2. Replace ``_chunks: list[str]`` with ``_accumulated: str`` updated
     in ``append()``; ``_build_renderable`` and ``_apply_markdown_swap``
     read the cached string directly.

These tests pin both invariants at the public-behaviour level.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── Perf F1 — timer is stopped after seal ────────────────────────────────────


@pytest.mark.asyncio
async def test_interval_handle_stored_on_mount() -> None:
    """Tier 2: ``on_mount`` stores the ``set_interval`` handle.

    Pinned at the public surface (the field is initialised to None in
    ``__init__``; it must hold a Timer after mount). A future refactor
    that re-introduces a bare ``self.set_interval(...)`` call would
    silently regress the seal-cancel path because there's no handle to
    stop.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("perf_handle_test", "reyn")
        await pilot.pause()
        assert row.interval_handle is not None, (
            "set_interval handle must be stored for seal() to stop later"
        )


@pytest.mark.asyncio
async def test_seal_stops_interval_handle() -> None:
    """Tier 2: ``seal()`` cancels the 16 ms render interval.

    Without this the timer kept firing 60 Hz callbacks into a sealed
    (or DOM-removed) widget for the rest of the session. After seal,
    the handle is cleared and the Textual Timer's ``_active`` Event
    is set (matching ``Timer.stop()`` semantics).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("perf_seal_test", "reyn")
        await pilot.pause()
        assert row.interval_handle is not None
        captured_handle = row.interval_handle

        row.append("hello")
        row.seal()
        await pilot.pause()

        assert row.interval_handle is None, "handle should be cleared after seal"
        # The captured handle's stop() flag should be set
        active_event = getattr(captured_handle, "_active", None)
        if active_event is not None and hasattr(active_event, "is_set"):
            assert active_event.is_set(), (
                "captured timer's _active should be set (= stop() ran)"
            )


@pytest.mark.asyncio
async def test_seal_before_mount_defers_cancel_to_on_mount() -> None:
    """Tier 2: a stream sealed before its widget mounted still cancels cleanly.

    The deferred-seal path (= ``seal()`` called between ``__init__`` and
    ``on_mount``) must end up in the same final state as the mounted
    path: handle is None, sealed is True. Pins the symmetry so a future
    refactor of one branch doesn't leave the other leaking.
    """
    from reyn.interfaces.tui.widgets.streaming_row import StreamingRow
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Manually construct a row WITHOUT mounting, seal it, then mount.
        row = StreamingRow(prefix="reyn", id="defer_seal_test")
        row.append("rapid burst")
        row.seal()
        assert row.sealed
        assert row.interval_handle is None  # never started

        conv.mount(row)
        await pilot.pause()

        # After on_mount: interval was started THEN stopped immediately
        # because _sealed was already True.
        assert row.interval_handle is None, (
            "deferred seal must stop the just-started interval in on_mount"
        )


# ── Perf F2 — accumulation is linear, no per-flush join ──────────────────────


@pytest.mark.asyncio
async def test_append_grows_accumulated_string_linearly() -> None:
    """Tier 2: each ``append(chunk)`` adds exactly that text to ``_accumulated``.

    Replaces the old ``_chunks: list[str]`` storage; pinning the new
    field directly catches accidental list-comprehension or join-based
    re-introductions.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("perf_linear_test", "reyn")

        chunks = ["alpha ", "beta ", "gamma"]
        for c in chunks:
            row.append(c)
        await pilot.pause()

        assert row.accumulated == "alpha beta gamma"
        assert row.full_text() == "alpha beta gamma", (
            "full_text() must read from _accumulated, not rebuild from a list"
        )


@pytest.mark.asyncio
async def test_no_chunks_list_attribute() -> None:
    """Tier 2: ``_chunks`` is gone; only ``_accumulated`` carries the body.

    Defence against partial revert: a future refactor that restores
    ``_chunks: list[str]`` would either duplicate state (= drift) or
    quietly re-introduce the O(N²) join. Pinning the absence ensures
    one source of truth.
    """
    from reyn.interfaces.tui.widgets.streaming_row import StreamingRow
    row = StreamingRow(prefix="r")
    assert not hasattr(row, "_chunks"), (
        "_chunks list was replaced by _accumulated; the list path "
        "must not be re-introduced"
    )


@pytest.mark.asyncio
async def test_build_renderable_uses_accumulated_string() -> None:
    """Tier 2: ``_build_renderable`` renders the cached string verbatim.

    No mid-render join. The rendered plain text should contain the
    accumulated body exactly between the prefix and the cursor /
    stall indicator.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("perf_body_test", "reyn")
        row.append("hello ")
        row.append("world")
        await pilot.pause()

        plain = row._build_renderable().plain
        assert "hello world" in plain, (
            f"rendered body must contain the accumulated string verbatim; "
            f"got {plain!r}"
        )
