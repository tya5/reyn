"""Tier 2: #5870 stage 2 (F2 + F3, architect ruling) — the drawer's per-frame
refresh only pays for what a frame could actually have changed.

**F2**: ``TextualChatApp._pane_rows`` used to compute BOTH ``self.
_history_turns()`` and ``self._artifact_rows()`` unconditionally on every
call, before ``pane_payload`` (the very next line) even looked at
``tab_id`` — even though ``pane_payload``'s own dispatch only ever reads
``history``/``artifacts`` for the two tab_ids named after it. Opening Ctx
or Help paid the full cost (a ``self.conversation`` walk, an artifact-ref
table resolution) for two values nothing downstream could ever use.

**F3**: ``TextualChatApp._refresh_live_chrome`` used to rebuild the OPEN
drawer pane on EVERY arriving frame, including one that provably carries
nothing but a status/snapshot delta (``StatusApplied`` — see that class's
own docstring: "carries nothing to apply of its own"). For the two panes
whose content is derived from ``self.conversation`` (never ``_snapshot()``'s
numeric fields) — History and Artifacts — such a frame cannot have changed
anything, so rebuilding is pure waste on the architect's own census.

Both fixes are tested here on the SAME REAL, mounted ``TextualChatApp`` —
observer subclasses that record which real method the production code
actually called (mirrors ``test_3338_tui_status_chrome_liveness.py``'s own
``_PaneRefreshCountingApp`` idiom: the real implementation still runs, the
subclass only counts), never a private-state read. Positive controls pair
every "this must NOT happen" assertion so an accidentally-vacuous negative
(e.g. the frame never reaching the pump at all) cannot pass silently.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, StatusApplied
from reyn.runtime.outbox import OutboxMessage


class _MixedTransport(ClientTransportStub):
    """A real :class:`ClientTransport` that can push either a real
    ``DisplayFrame`` (an ``OutboxMessage``) or a raw ``StatusApplied()``
    item — mirrors #5830's own ``_EventOnlyTransport``
    (``test_3338_tui_status_chrome_liveness.py``), generalized to cover
    both frame families this file's own F2/F3 tests need to distinguish
    between."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def push_status_applied(self) -> None:
        await self._queue.put(StatusApplied())

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> None:
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _PaneRefreshCountingApp:
    """Mirrors ``test_3338_tui_status_chrome_liveness.py``'s own
    ``_PaneRefreshCountingApp`` exactly: an OBSERVER, never a substitute —
    ``_refresh_pane``'s real body still runs, so what is counted is the
    production call, not a faked one."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.refreshed_panes: "list[str]" = []

    def _refresh_pane(self, tab_id, *args, **kwargs):  # type: ignore[override]
        self.refreshed_panes.append(tab_id)
        return super()._refresh_pane(tab_id, *args, **kwargs)  # type: ignore[misc]


class _HistoryArtifactCountingApp:
    """Same observer shape as :class:`_PaneRefreshCountingApp`, for the TWO
    specific methods F2 makes conditional — real bodies still run."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.history_turns_calls = 0
        self.artifact_rows_calls = 0

    def _history_turns(self, *args, **kwargs):  # type: ignore[override]
        self.history_turns_calls += 1
        return super()._history_turns(*args, **kwargs)  # type: ignore[misc]

    def _artifact_rows(self, *args, **kwargs):  # type: ignore[override]
        self.artifact_rows_calls += 1
        return super()._artifact_rows(*args, **kwargs)  # type: ignore[misc]


# ── F2: history/artifacts computed only for the tab that reads them ─────────


@pytest.mark.asyncio
async def test_ctx_tab_open_never_computes_history_or_artifacts() -> None:
    """Tier 2: F2, architect census — with Ctx open, a display frame
    arriving never calls ``_history_turns``/``_artifact_rows`` at all.
    Paired with a positive control (History open, the SAME kind of frame
    DOES call ``_history_turns``) so the zero above is a real absence, not
    a dead code path."""

    class _CountingApp(_HistoryArtifactCountingApp, TextualChatApp):
        pass

    transport = _MixedTransport()
    app = _CountingApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("ctx")
        await pilot.pause()
        app.history_turns_calls = 0
        app.artifact_rows_calls = 0

        await transport.push_display(OutboxMessage(kind="agent", text="hello"))
        await pilot.pause()
        await pilot.pause()

        assert app.history_turns_calls == 0, (
            f"_history_turns was called {app.history_turns_calls} times "
            "with Ctx open — F2 must make this conditional on tab_id"
        )
        assert app.artifact_rows_calls == 0, (
            f"_artifact_rows was called {app.artifact_rows_calls} times "
            "with Ctx open — F2 must make this conditional on tab_id"
        )

        # Positive control: History open, the same kind of frame DOES call
        # _history_turns — proves the zero above is not a dead code path
        # (e.g. the frame never reaching the pump at all).
        app._open_drawer("history")
        await pilot.pause()
        app.history_turns_calls = 0

        await transport.push_display(OutboxMessage(kind="agent", text="world"))
        await pilot.pause()
        await pilot.pause()

        assert app.history_turns_calls > 0, (
            "the positive control did not fire — _history_turns was never "
            "called even with History open"
        )


@pytest.mark.asyncio
async def test_artifacts_tab_open_calls_artifact_rows_at_most_once_per_refresh() -> None:
    """Tier 2: F2 — with Artifacts open, one frame's own refresh calls
    ``_artifact_rows`` exactly once (not twice — the pre-fix shape computed
    it once inside the old unconditional ``_pane_rows`` call and AGAIN,
    separately, inside ``_refresh_pane`` itself for the command-list/row-
    cache pair)."""

    class _CountingApp(_HistoryArtifactCountingApp, TextualChatApp):
        pass

    transport = _MixedTransport()
    app = _CountingApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("artifacts")
        await pilot.pause()
        app.artifact_rows_calls = 0

        await transport.push_display(OutboxMessage(kind="agent", text="hello"))
        await pilot.pause()
        await pilot.pause()

        assert app.artifact_rows_calls == 1, (
            f"expected exactly one _artifact_rows call for one frame's "
            f"refresh, got {app.artifact_rows_calls} — the row list is "
            "being derived twice for the same refresh"
        )


# ── F3: a StatusApplied-only frame cannot change History/Artifacts ──────────


@pytest.mark.asyncio
async def test_history_tab_open_status_only_frame_skips_the_pane_refresh() -> None:
    """Tier 2: F3, architect ruling — with History open, a frame carrying
    ONLY a status/snapshot delta (``StatusApplied``) does not trigger
    ``_refresh_pane("history")`` at all — the pane's own
    ``clear_options``/``add_options`` never runs for a frame that cannot
    have added a conversation entry. Paired with a positive control (a
    REAL display frame, same pane, DOES refresh) so the skip above is not
    vacuous."""

    class _CountingApp(_PaneRefreshCountingApp, TextualChatApp):
        pass

    transport = _MixedTransport()
    app = _CountingApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("history")
        await pilot.pause()
        app.refreshed_panes.clear()

        await transport.push_status_applied()
        await pilot.pause()
        await pilot.pause()

        assert "history" not in app.refreshed_panes, (
            f"a StatusApplied-only frame re-rendered the History pane: "
            f"{app.refreshed_panes}"
        )

        # Positive control: a real display frame, same open pane, DOES
        # refresh — the skip above is a real absence, not a dead pump.
        await transport.push_display(OutboxMessage(kind="agent", text="hi"))
        await pilot.pause()
        await pilot.pause()

        assert "history" in app.refreshed_panes, (
            "the positive control did not fire — the History pane was "
            "never refreshed even by a real display frame"
        )


@pytest.mark.asyncio
async def test_artifacts_tab_open_status_only_frame_skips_the_pane_refresh() -> None:
    """Tier 2: F3 — same shape as the History test above, for Artifacts —
    the OTHER pane named in ``_STATUS_INDEPENDENT_PANES``."""

    class _CountingApp(_PaneRefreshCountingApp, TextualChatApp):
        pass

    transport = _MixedTransport()
    app = _CountingApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("artifacts")
        await pilot.pause()
        app.refreshed_panes.clear()

        await transport.push_status_applied()
        await pilot.pause()
        await pilot.pause()

        assert "artifacts" not in app.refreshed_panes, (
            f"a StatusApplied-only frame re-rendered the Artifacts pane: "
            f"{app.refreshed_panes}"
        )


@pytest.mark.asyncio
async def test_cost_tab_open_status_only_frame_still_refreshes() -> None:
    """Tier 2: F3 non-vacuity — a status-DRIVEN pane (Cost, reading
    ``_snapshot()``'s own numeric fields) DOES still refresh on a
    StatusApplied-only frame. Proves the skip in the two tests above is
    scoped to the two panes named in ``_STATUS_INDEPENDENT_PANES``, never
    a blanket "skip whenever the frame is StatusApplied" — the architect's
    own "skip only what is certain" ruling."""

    class _CountingApp(_PaneRefreshCountingApp, TextualChatApp):
        pass

    transport = _MixedTransport()
    app = _CountingApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("cost")
        await pilot.pause()
        app.refreshed_panes.clear()

        await transport.push_status_applied()
        await pilot.pause()
        await pilot.pause()

        assert "cost" in app.refreshed_panes, (
            "a StatusApplied frame must still refresh a status-driven pane "
            "(Cost) — this is exactly what such a frame CAN move"
        )
