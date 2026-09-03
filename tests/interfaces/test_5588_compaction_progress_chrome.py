"""Tier 2: #5588 — the shrink-flow progress ROW is a single flowview entry
(``TextualChatApp._compaction_progress_entry``), mounted in a REAL
``TextualChatApp`` and driven from a REAL ``Session``'s own snapshot (the
production ``interfaces/repl/status.py::_snapshot_for_session()`` seam, not
a hand-typed dict).

owner (real-machine, 2026-09-02/03): "スピナーになってないね"／"進捗も見え
ない"／"開始〜終了までスピナー対応して...開始〜進捗〜終了は単一 flowview
entry or group にして" — this file replaces its own prior generation
(``CompactionProgressRow``, a standalone chrome ``Static`` sibling of
``MenuBar``, REMOVED by this same PR) with tests against the flowview entry
that took its place: one ``Entry[OutboxMessage]``, created once, its
``meta`` updated in place while running (never re-created), spinning via
the SAME live-indicator convention a RUNNING tool-call row already uses,
and settled to a terminal ``EntryState`` exactly once.

Real ``Session``/``AgentRegistry``/``EventLog`` throughout. Driving via a
direct ``session._audit_events.emit(...)`` call for
``compaction_shrink_recovered``/``llm_request`` is the same legitimate
"arrange, not assert" pattern established across #5557/#5588's own prior
tests: this file's claim is "the entry renders what the snapshot carries",
not "production fires these events in scenario X" (that belongs to
tests/services/test_5592_observability_fields.py). Setting
``session._compaction_controller._compacting`` directly (no public setter
exists — see ``CompactionController.is_compacting``'s own read-only
property) is the same class of legitimate scenario-arrangement, not an
assertion on private state.

Assertions read ``entry.item.meta`` (the INTEGER/bool/str figures the row
is built from — same public-surface precedent ``presenter.py``'s own
``_pipeline_row`` established: "the numbers are what the row is actually
about", never a parse of rendered text/pixels) and ``entry.state``
(``EntryState`` — a public flowview attribute, not private state).

``_MutableSnapshotReadModel``/``_EventOnlyTransport`` mirror
``test_3338_tui_status_chrome_liveness.py``'s own classes of the same name
byte-for-byte (each TUI test file keeps its own local copy, per that
file's own established convention — no cross-test-file import)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import Event
from tests._support.agent_session import make_session
from tests._support.events import settle


class _MutableSnapshotReadModel(ChatReadModel):
    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, snap: dict) -> None:
        self.snap = snap

    def snapshot(self, config=None):
        return self.snap

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_5588_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class _EventOnlyTransport(ClientTransportStub):
    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

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


async def _make_app_with_real_session(tmp_path: Path):
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            registry=holder.get("reg"),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("alpha")
    session = reg.get_or_load("alpha")
    snap = _snapshot_for_session(reg, session)
    app = TextualChatApp(
        transport=_EventOnlyTransport(), read_model=_MutableSnapshotReadModel(snap),
    )
    return app, session, reg


def _open_compaction_entry(app: TextualChatApp):
    """Tier 2's own public-surface read of "is there a shrink-flow-episode
    entry open right now" — never ``app._compaction_progress_entry``
    (private state; the AST gate correctly flags a syntactic position, not
    just an assertion on it — testing.md: "naming a syntactic position only
    moves the read one line up"). ``app.conversation.entries`` (a public
    ``FlowModel`` property) is the SAME surface :func:`presenter.py`'s own
    ``_pipeline_row`` precedent reads ("the numbers are what the row is
    actually about") — scanning it for the ONE entry tagged
    :data:`_COMPACTION_PROGRESS_KEY` is genuinely public, not a renamed
    private read."""
    from reyn.interfaces.inline.textual_chat._meta_keys import COMPACTION_PROGRESS_KEY

    for entry in app.conversation.entries:
        if entry.item.meta.get(COMPACTION_PROGRESS_KEY) is not None:
            return entry
    return None


@pytest.mark.asyncio
async def test_no_entry_while_not_compacting(tmp_path: Path) -> None:
    """Tier 2: accept/deny — no flowview entry exists while nothing is
    compacting (the issue's own deny criterion: never a persistently-
    visible row)."""
    app, _session, _reg = await _make_app_with_real_session(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert _open_compaction_entry(app) is None


@pytest.mark.asyncio
async def test_entry_shows_real_events_driven_snapshot(tmp_path: Path) -> None:
    """Tier 2: accept — a CONTROLLER-driven compaction (the threshold pass /
    force_compact_now) creates the flowview entry, RUNNING, from the App's
    own refresh cycle end-to-end from Session through to the entry rather
    than the pure render layer in isolation.

    #5618 narrowed what this can claim. The rung figures
    (raw_middle_remaining, upstream_recovery_call_count) are measured by the
    retry LADDER and are now joined against the recovery episode they were
    measured in — a controller compaction is a different operation and runs
    in no episode, so those figures correctly read unknown here and the
    entry's own meta does not carry them. The arrangement below (controller
    flag up, ladder events on the log, no episode) is one production cannot
    produce at all: the engine emits those events from inside retry_loop,
    which only runs inside an episode.

    So this asserts the controller path's real contract — the entry appears
    and says the honest thing — and, as the deny half, that it does NOT
    carry rung numbers it never measured.

    Accept sibling (architect, #5630): the figures ARE rendered on the path
    that measures them — ``test_the_measured_figures_are_reported_while_their_
    episode_runs`` in tests/runtime/test_5618_recovery_episode_gate.py, which
    drives the real ladder end-to-end. Without that sibling, this test's deny
    half would be satisfied by an entry that never carries the figures at
    all."""
    from reyn.interfaces.inline.textual_chat._meta_keys import RUNNING_SINCE_KEY

    app, session, reg = await _make_app_with_real_session(tmp_path)
    session._compaction_controller._compacting = True  # arrange: no public setter
    session._audit_events.emit(
        "compaction_shrink_recovered",
        cause="ContextOverflowError", iteration=0, consecutive=1,
        t_max_override=None, raw_middle_remaining=2464, raw_middle_total=2469,
    )
    session._audit_events.emit(
        "llm_request", model="x", input_chars=100,
        max_input_tokens_applied=8000, upstream_recovery_call_count=43,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Re-read the snapshot NOW (post-emit) — _snapshot() runs fresh
        # every render frame in production; this test's own snap dict must
        # be refreshed the same way, not reused stale from before the emits.
        fresh_snap = _snapshot_for_session(reg, session)
        app._read_model.snap = fresh_snap
        app._refresh_compaction_progress()
        await pilot.pause()

        entry = _open_compaction_entry(app)
        assert entry is not None
        from textual_flowview import EntryState
        assert entry.state is EntryState.RUNNING
        meta = entry.item.meta
        assert meta.get("is_compacting") is True
        assert meta.get(RUNNING_SINCE_KEY) is not None, "expected the spinner to be running"
        assert meta.get("spill_done") is None, (
            f"a controller-driven compaction measures no ladder rung figures — "
            f"those numbers would be another operation's, shown as this one's "
            f"progress: {meta!r}"
        )
        assert meta.get("call_count") is None, meta


@pytest.mark.asyncio
async def test_entry_settles_success_when_compaction_stops_with_no_new_failure(
    tmp_path: Path,
) -> None:
    """Tier 2: #5588 acceptance ③ — the entry settles to SUCCESS exactly
    once when ``is_compacting`` goes back to False with no NEW
    router_context_overflow_unrecovered since it started (a genuine
    resolve, not a fabricated failure)."""
    from textual_flowview import EntryState

    app, session, reg = await _make_app_with_real_session(tmp_path)
    session._compaction_controller._compacting = True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()
        entry = _open_compaction_entry(app)
        assert entry is not None, "arrange: entry must exist before it can settle"

        session._compaction_controller._compacting = False
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()

        # The entry stays in the flow (a settled row, matching a completed
        # tool call's own "still there after settling" contract) — the
        # public witness for "settled" is its OWN state, not disappearance.
        assert _open_compaction_entry(app) is entry
        assert entry.state is EntryState.SUCCESS
        assert entry.item.meta.get("terminal_text") is None, (
            "a success settle must never carry a failure sentence"
        )


@pytest.mark.asyncio
async def test_entry_settles_error_with_the_real_retry_loop_terminal_text(
    tmp_path: Path,
) -> None:
    """Tier 2: #5588 acceptance ③ — a NEW router_context_overflow_
    unrecovered fired since the entry was created settles it ERROR, with
    the resolved RetryLoopTerminal sentence (never a parsed reason
    string)."""
    from textual_flowview import EntryState

    app, session, reg = await _make_app_with_real_session(tmp_path)
    session._compaction_controller._compacting = True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()
        entry = _open_compaction_entry(app)
        assert entry is not None

        session._audit_events.emit(
            "router_context_overflow_unrecovered",
            error="UnrecoveredError(...)", terminal="mid_floor",
        )
        await settle(session._audit_events)  # dispatch is queued under a real event loop
        session._compaction_controller._compacting = False
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()

        assert _open_compaction_entry(app) is entry, "the settled row stays in the flow"
        assert entry.state is EntryState.ERROR
        assert entry.item.meta.get("terminal_text") == "1つのやり取りが単独で大きすぎます"


@pytest.mark.asyncio
async def test_a_stale_terminal_from_before_the_entry_started_is_never_reused(
    tmp_path: Path,
) -> None:
    """Tier 2: deny side (acceptance ③'s own load-bearing property) — a
    terminal_seq that was already at its baseline WHEN the entry was
    created must never make a genuinely-successful episode settle as
    ERROR. Without the baseline comparison, an old failure from a
    PREVIOUS episode would leak into this one's own settle."""
    from textual_flowview import EntryState

    app, session, reg = await _make_app_with_real_session(tmp_path)
    # An OLDER episode's own failure, already on the log before this
    # entry is ever created.
    session._audit_events.emit(
        "router_context_overflow_unrecovered", error="old", terminal="room_floor",
    )
    session._compaction_controller._compacting = True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()
        entry = _open_compaction_entry(app)
        assert entry is not None

        # No NEW failure this time — the episode genuinely resolves.
        session._compaction_controller._compacting = False
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()

        assert entry.state is EntryState.SUCCESS, (
            "a terminal_seq already at baseline when the entry was created "
            "must never be read as a NEW failure"
        )


@pytest.mark.asyncio
async def test_compaction_episode_marker_frame_absorbs_into_the_open_entry(
    tmp_path: Path,
) -> None:
    """Tier 2: #5588 — a lifecycle_forwarder marker tagged
    ``compaction_episode_marker`` (on_compaction_started/completed/failed)
    is absorbed into the single open episode entry rather than appended
    as its own conv-pane row, TUI-locally, while an entry is open."""
    from reyn.runtime.outbox import OutboxMessage

    app, session, reg = await _make_app_with_real_session(tmp_path)
    session._compaction_controller._compacting = True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._read_model.snap = _snapshot_for_session(reg, session)
        app._refresh_compaction_progress()
        await pilot.pause()
        assert _open_compaction_entry(app) is not None

        before = list(app.conversation.entries)
        result = app._ingest_frame(OutboxMessage(
            kind="system", text="[⟳ compacting 3 turns]",
            meta={"compaction_episode_marker": True},
        ))
        after = list(app.conversation.entries)

        assert result is None, "an absorbed frame creates no new entry"
        assert len(after) == len(before), (
            f"expected the marker to absorb (no new row) — root count grew "
            f"{len(before)} -> {len(after)}"
        )


@pytest.mark.asyncio
async def test_compaction_episode_marker_frame_appends_normally_with_no_open_entry(
    tmp_path: Path,
) -> None:
    """Tier 2: deny side — with NO open episode entry (a REMOTE reconnect
    mid-episode, or the entry already settled), a tagged marker falls
    through and appends as its own row rather than being silently
    dropped."""
    from reyn.runtime.outbox import OutboxMessage

    app, _session, _reg = await _make_app_with_real_session(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert _open_compaction_entry(app) is None

        before = list(app.conversation.entries)
        result = app._ingest_frame(OutboxMessage(
            kind="system", text="[⟳ compacting 3 turns]",
            meta={"compaction_episode_marker": True},
        ))
        after = list(app.conversation.entries)

        assert result is not None, "no open entry to absorb into -- must append"
        assert len(after) == len(before) + 1
