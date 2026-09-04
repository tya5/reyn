"""Tier 1/2: #5654 — the TUI Task tab (`/tasks`' UI half).

`task_pane_entries` is a pure render function over the snapshot's own
`tasks`/`tasks_reported` keys (#3987/#5009's own declared-capability shape:
a remote connection's `tasks` is always `[]`, indistinguishable on its own
from a genuinely task-free LOCAL session — `tasks_reported` is what tells
them apart). Cancellation reaches the substrate through the SAME
`/tasks cancel <id>` slash command a typed one would (`RewindPicker`'s own
established shape — a widget posts a Message, the App maps a selected row's
command straight through `self._submit`, never a private action path).

Real `TextualChatApp` where a mounted widget is under test; pure snapshot
dicts elsewhere — no mocks.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reyn.interfaces.inline.textual_chat.chrome import (
    _MENU_TABS,
    pane_commands,
    pane_payload,
    task_pane_entries,
)
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    ChatReadModel,
    project_remote_snapshot,
    reported_snapshot_keys,
)

_NOW = datetime(2026, 9, 2, 5, 0, 0, tzinfo=timezone.utc)


def _task(**overrides) -> dict:
    base = {
        "task_id": "abc123", "kind": "prompt", "target": "beta",
        "registered_at": "2026-09-02T04:59:00+00:00",
        "cancel_requested_at": None, "cancellable": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# capability declaration (#5009/#3987 shape)
# ---------------------------------------------------------------------------


def test_capabilities_declare_tasks_reported():
    """Tier 1: the declaration itself."""
    assert LOCAL_CHAT_READ_CAPABILITIES.tasks_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.tasks_reported is False


def test_the_shared_helper_derives_tasks_reported():
    """Tier 1: pins reported_snapshot_keys's own projection for this field."""
    assert reported_snapshot_keys(LOCAL_CHAT_READ_CAPABILITIES)["tasks_reported"] is True
    assert reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)["tasks_reported"] is False


def test_remote_snapshot_declares_tasks_unreported():
    """Tier 1: the real producer carries the declaration through, paired
    with the graceful `[]` the key already carried."""
    snap = project_remote_snapshot({})
    assert snap["tasks_reported"] is False
    assert snap["tasks"] == []


# ---------------------------------------------------------------------------
# render — Tier 1, pure
# ---------------------------------------------------------------------------


def test_not_reported_shows_the_standard_marker_not_a_fabricated_empty():
    """Tier 1: deny. Strip-falsifier: gate this on `snap.get("tasks_reported")` truthily
    (a bare `snap.get("tasks")` check) and this test goes red — a remote
    connection's `[]` would render as "no running tasks" instead of "not
    reported", the exact #5009 conflation this field exists to close."""
    entries = task_pane_entries({"tasks_reported": False, "tasks": []})
    assert entries == [("not reported on this connection", "")]


def test_none_snapshot_degrades_the_same_as_not_reported():
    """Tier 1: the genuine pre-attach None case degrades gracefully, never
    crashing and never claiming reporting with nothing to consult."""
    assert task_pane_entries(None) == [("not reported on this connection", "")]


def test_reported_but_empty_shows_a_distinct_deny_text_not_zero_rows():
    """Tier 1: deny, deliberate. zero rows and 'not reported' must render
    as visibly DIFFERENT things — an empty OptionList and a marker row are
    not interchangeable to an operator glancing at the tab."""
    entries = task_pane_entries({"tasks_reported": True, "tasks": []})
    assert entries == [("実行中の task はありません", "")]


def test_a_running_prompt_task_renders_kind_target_elapsed_and_cancel_command():
    """Tier 1: accept — the full row shape for a live, cancellable prompt
    task."""
    entries = task_pane_entries(
        {"tasks_reported": True, "tasks": [_task()]}, now=_NOW,
    )
    (row,) = entries
    text, command = row
    assert "prompt" in text and "beta" in text
    assert "1m00s" in text, f"elapsed must be computed from now-registered_at: {text!r}"
    assert command == "/tasks cancel abc123"


def test_a_pipeline_tasks_target_cell_is_the_pipeline_name():
    """Tier 1: accept — a pipeline task's target cell is its own name
    (`original_request`), architect's corrected design (2026-09-02)."""
    entries = task_pane_entries(
        {"tasks_reported": True, "tasks": [
            _task(task_id="p1", kind="pipeline", target="nightly_backup"),
        ]},
        now=_NOW,
    )
    (row,) = entries
    assert "pipeline" in row[0] and "nightly_backup" in row[0]


def test_an_unresolvable_target_reads_a_placeholder_never_a_guess():
    """Tier 1: deny. target=None (#3978's own |waiting_on|!=1 case, or an unpopulated
    pipeline original_request) must never be fabricated into a name."""
    entries = task_pane_entries(
        {"tasks_reported": True, "tasks": [_task(target=None)]}, now=_NOW,
    )
    assert "?" in entries[0][0]


def test_a_crash_recovered_task_is_not_cancellable_and_carries_no_command():
    """Tier 1: deny — a restored handle (cancel=None) offers no cancel
    affordance to select, rather than a command that would silently no-op."""
    entries = task_pane_entries(
        {"tasks_reported": True, "tasks": [_task(cancellable=False)]}, now=_NOW,
    )
    text, command = entries[0]
    assert "中断不可" in text
    assert command == ""


def test_a_cancel_already_requested_task_shows_the_note_but_keeps_its_command():
    """Tier 1: accept. The command is NOT withdrawn once a cancel is requested (repeat
    cancellation of an already-cancelling task is a no-op the op layer
    itself handles, not something the pane needs to prevent by hiding the
    row's affordance)."""
    entries = task_pane_entries(
        {"tasks_reported": True, "tasks": [
            _task(cancel_requested_at="2026-09-02T04:59:30+00:00"),
        ]},
        now=_NOW,
    )
    text, command = entries[0]
    assert "中断要求済" in text
    assert command == "/tasks cancel abc123"


def test_elapsed_is_omitted_never_fabricated_when_now_is_not_given():
    """Tier 1: deny. A caller with no clock at hand (e.g. a test exercising only the row
    shape) gets a row with no elapsed segment — never a fabricated 0s."""
    entries = task_pane_entries({"tasks_reported": True, "tasks": [_task()]})
    assert "s" not in entries[0][0].split("·")[-1] or "中断" in entries[0][0], entries[0][0]
    # More directly: no digit-then-"s" elapsed token appears at all.
    import re
    assert not re.search(r"\d+m?\d*s", entries[0][0]), entries[0][0]


def test_pane_payload_and_pane_commands_stay_index_aligned():
    """Tier 1: the invariant every actionable pane depends on (DrawerRow's own
    docstring): `pane_payload`'s rows and `pane_commands`'s commands come
    from the SAME entries list, so an OptionList selection index can never
    address a different task than the one highlighted."""
    snap = {"tasks_reported": True, "tasks": [
        _task(task_id="t1"), _task(task_id="t2", cancellable=False),
    ]}
    rows = pane_payload("task", snapshot=snap, now=_NOW)
    cmds = pane_commands("task", snap, now=_NOW)
    assert len(rows) == len(cmds), (
        f"rows and commands must stay index-aligned: {rows!r} vs {cmds!r}"
    )
    assert cmds[0] == "/tasks cancel t1"
    assert cmds[1] == ""


def test_task_tab_is_registered_and_in_the_actionable_list_panes():
    """Tier 1: the Task tab exists, is labeled, and is actionable (not a
    read-only readout like Pipe/Cron)."""
    from reyn.interfaces.inline.textual_chat.chrome import pane_is_list

    assert dict(_MENU_TABS)["task"] == "Task"
    assert pane_is_list("task") is True


# ---------------------------------------------------------------------------
# chrome — real App, real mounted widget
# ---------------------------------------------------------------------------


class _TaskSnapshotReadModel(ChatReadModel):
    """Minimal real ChatReadModel returning a fixed snapshot — mirrors the
    convention every other real-App chrome test in this package uses
    (``_MutableSnapshotReadModel`` et al.), reproduced here rather than
    imported to keep this file's fixture surface self-contained.

    #5729 (lead-coder's #5734 review, real CI red): this class did NOT
    inherit ``ChatReadModel`` before — a bare duck-type — so it never
    picked up the ABC's new ``add_status_listener``/``remove_status_
    listener`` no-op defaults, and ``TextualChatApp.on_unmount``'s
    unconditional call to the latter crashed with ``AttributeError`` on
    real CI. Inheriting the real ABC closes the class properly rather
    than adding a defensive ``getattr`` in ``app.py``."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, snap: dict) -> None:
        self._snap = snap

    def snapshot(self, config=None):
        return dict(self._snap)

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self):
        from pathlib import Path
        return Path("/tmp/reyn_5654_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


@pytest.mark.asyncio
async def test_the_task_tab_shows_two_running_tasks_in_a_real_app():
    """Tier 2: chrome — a real, mounted TextualChatApp renders both a
    prompt and a pipeline task's rows from one snapshot."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp
    from reyn.interfaces.transport.client_transport import ClientTransportStub

    class _Transport(ClientTransportStub):
        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def frames(self):
            import asyncio
            await asyncio.Event().wait()
            yield  # pragma: no cover — unreachable, satisfies the generator shape

        async def submit_user_text(self, text: str) -> None:
            pass

        async def run_slash_command(self, name: str, args: str) -> bool:
            return True

        async def answer_intervention_text(self, text: str) -> bool:
            return False

        async def answer_intervention_choice(self, choice_id: str) -> bool:
            return False

        def has_session(self) -> bool:
            return True

        def pending_intervention_head(self):
            return None

        def put_display(self, msg) -> None:
            pass

        async def clear_pending_command_ui(self) -> None:
            pass

        async def cancel_inflight(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    snap = {
        "tasks_reported": True,
        "tasks": [
            _task(task_id="t1", target="beta"),
            _task(task_id="t2", kind="pipeline", target="nightly_backup"),
        ],
    }
    app = TextualChatApp(transport=_Transport(), read_model=_TaskSnapshotReadModel(snap))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        rows = app._pane_rows("task")
        assert any("beta" in r for r in rows), rows
        assert any("nightly_backup" in r for r in rows), rows
