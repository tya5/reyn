"""Tier 2: #5588 — the shrink-flow progress row, mounted in a REAL
``TextualChatApp`` and driven from a REAL ``Session``'s own snapshot (the
production ``interfaces/repl/status.py::_snapshot_for_session()`` seam, not
a hand-typed dict).

Real ``Session``/``AgentRegistry``/``EventLog`` throughout. Driving via a
direct ``session._audit_events.emit(...)`` call for
``compaction_shrink_recovered``/``llm_request`` is the same legitimate
"arrange, not assert" pattern established across #5557/#5588's own prior
tests: this file's claim is "the chrome row renders what the snapshot
carries", not "production fires these events in scenario X" (that belongs
to tests/services/test_5592_observability_fields.py). Setting
``session._compaction_controller._compacting`` directly (no public setter
exists — see ``CompactionController.is_compacting``'s own read-only
property) is the same class of legitimate scenario-arrangement, not an
assertion on private state.

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
from reyn.interfaces.inline.textual_chat.compaction_progress import CompactionProgressRow
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import Event
from tests._support.agent_session import make_session


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


@pytest.mark.asyncio
async def test_row_absent_while_not_compacting(tmp_path: Path) -> None:
    """Tier 2: accept/deny — the row is mounted but hidden (the issue's
    own deny criterion: never a persistently-visible row)."""
    app, _session, _reg = await _make_app_with_real_session(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        row = app.query_one(CompactionProgressRow)
        assert row.display is False


@pytest.mark.asyncio
async def test_row_shows_real_events_driven_snapshot(tmp_path: Path) -> None:
    """Tier 2: accept — with is_compacting=True and real
    compaction_shrink_recovered/llm_request events on the session's own
    audit log, the mounted row renders the exact text
    compaction_progress_lines() produces from that real data — an
    end-to-end proof from Session through the App's own refresh cycle to
    the rendered widget, not just the pure render layer in isolation."""
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

        row = app.query_one(CompactionProgressRow)
        assert row.display is True
        assert row.lines[0] == "⟳ 文脈を縮めています（自動で終わります）"
        assert "① 退避 5/2469  呼び出し 43" in row.lines[-1]
