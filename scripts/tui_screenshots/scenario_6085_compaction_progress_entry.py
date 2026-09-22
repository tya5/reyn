"""#6085 scenario: one compaction episode's start -> progress -> end folded
into a SINGLE flowview entry, with ordinary chat rows before and after it
so the folding is visible in context (not just "one row on an otherwise
empty screen").

Issue #6085's own question (lead-coder, to owner): does this shape ("1
compaction = 1 row") satisfy #5588's owner request ("開始〜進捗〜終了は単一
flowview entry or group にして")? That question can only be answered from a
picture — this scenario builds it.

Reuses `tests/interfaces/test_5588_compaction_progress_chrome.py`'s own
established idioms verbatim (no new mechanism invented, per lead-coder's
brief):

  - a REAL ``Session``/``AgentRegistry``/``StateLog`` (``_make_app_with_
    real_session`` there), read through the production ``_snapshot_for_
    session()`` seam -- never a hand-typed status dict for the compaction
    figures themselves.
  - ``session._compaction_controller._compacting = True`` / ``False`` is
    the SAME "arrange, not assert" idiom that file's own tests use (no
    public setter exists -- see ``CompactionController.is_compacting``'s
    own read-only property).
  - ``app._refresh_compaction_progress()`` after re-reading the snapshot
    is the SAME per-frame trigger production uses on every arriving frame
    (see that method's own docstring) -- never a private field poke.
  - the mid-episode marker frame (``compaction_episode_marker`` +
    ``compaction_episode_seq``) absorbing into the SAME open entry is the
    exact shape ``test_compaction_episode_marker_frame_absorbs_into_the_
    open_entry`` asserts.

Ordinary chat rows (one exchange before, one after) go through
``QueueTransport.push`` -- the same real frame-ingestion path
``scenario_4535_image_present.py``/``scenario_6077_wire_fifo_status.py``
already use -- so the captured frame shows the compaction row sitting
inside a normal conversation, not floating alone.

No `sleep` anywhere: state transitions are driven by direct real-session
field/emit calls (the same ones the test file already established as
legitimate arrangement) and settled with `pilot.pause()`, never a timer.

CI: manual -- imported by tui_screenshot.py, never run directly.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# #3024: this module imports `reyn` too (independent of whether the caller
# already guarded) -- see verify_env_identity.py's own guard_bare_script_or_
# exit docstring. `tui_screenshot.py` (the only sanctioned caller) already
# runs this before importing any scenario module, but this call stays here
# too: a scenario module is itself a `scripts/*.py`-shaped file per that
# guard's own contract, and this repeats at negligible cost (a single
# find_spec probe) rather than relying on caller discipline.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT), str(_REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from verify_env_identity import guard_bare_script_or_exit  # noqa: E402

guard_bare_script_or_exit()

from reyn.core.events.state_log import StateLog  # noqa: E402
from reyn.interfaces.inline.textual_chat import TextualChatApp  # noqa: E402
from reyn.interfaces.repl.read_model import (  # noqa: E402
    LOCAL_CHAT_READ_CAPABILITIES,
    ChatReadModel,
)
from reyn.interfaces.repl.status import _snapshot_for_session  # noqa: E402
from reyn.runtime.outbox import OutboxMessage  # noqa: E402
from reyn.runtime.registry import AgentRegistry  # noqa: E402
from tests._support.agent_session import make_session  # noqa: E402
from tests._support.textual_chat_test_helpers import QueueTransport  # noqa: E402

SIZE = (100, 34)

# A tmp dir the state log / session snapshot files live under -- discarded
# on interpreter exit (this script's own process is short-lived), never
# cleaned up mid-run since the App keeps the real Session alive throughout
# `direct()`.
_TMPDIR = tempfile.mkdtemp(prefix="reyn_6085_screenshot_")


class _MutableSnapshotReadModel(ChatReadModel):
    """Mirrors `test_5588_compaction_progress_chrome.py`'s own class of the
    same name byte-for-byte (each TUI screenshot/test file keeps its own
    local copy, per that file's established convention) -- wraps a snap
    dict this scenario re-reads fresh off the real Session before each
    `_refresh_compaction_progress()` call, exactly as that test does."""

    def __init__(self, snap: dict) -> None:
        self.snap = snap

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

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
        return Path("/tmp/reyn_6085_screenshot_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


_holder: dict = {}


def make_app() -> TextualChatApp:
    tmp_path = Path(_TMPDIR)
    state_log = StateLog(tmp_path / "state.wal")

    def _factory(profile):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            registry=_holder.get("reg"),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    _holder["reg"] = reg
    reg.create("alpha")
    session = reg.get_or_load("alpha")
    snap = _snapshot_for_session(reg, session)

    _holder["reg"] = reg
    _holder["session"] = session
    _holder["read_model"] = _MutableSnapshotReadModel(snap)

    return TextualChatApp(
        transport=QueueTransport(), read_model=_holder["read_model"],
    )


def _refresh(app: TextualChatApp) -> None:
    """Re-read the real Session's snapshot NOW (mirrors the test file's own
    comment: "_snapshot() runs fresh every render frame in production;
    this test's own snap dict must be refreshed the same way") and drive
    the SAME per-frame trigger production uses."""
    reg = _holder["reg"]
    session = _holder["session"]
    _holder["read_model"].snap = _snapshot_for_session(reg, session)
    app._refresh_compaction_progress()  # noqa: SLF001


async def direct(app: TextualChatApp, pilot) -> None:
    transport: QueueTransport = app._transport  # type: ignore[assignment]
    session = _holder["session"]

    # Context BEFORE the compaction episode -- an ordinary exchange, through
    # the real frame-ingestion path, so the captured frame shows this row
    # sitting inside a normal conversation, not floating alone.
    await transport.push(OutboxMessage(kind="user", text="keep going with the refactor"))
    await pilot.pause()
    await transport.push(
        OutboxMessage(kind="agent", text="working on it -- context is getting long, one moment", meta={"chain_id": "c1"})
    )
    await pilot.pause()

    # START: the controller begins compacting (same "arrange, not assert"
    # idiom the test file uses -- no public setter exists).
    session._compaction_controller._compacting = True  # noqa: SLF001
    _refresh(app)
    await pilot.pause()

    # PROGRESS: real audit events land while the episode is still open --
    # the SAME shape `test_entry_shows_real_events_driven_snapshot` drives
    # -- and the entry's meta re-derives from them on the next refresh,
    # never creating a second row.
    session._audit_events.emit(
        "compaction_shrink_recovered",
        cause="ContextOverflowError", iteration=0, consecutive=1,
        t_max_override=None, raw_middle_remaining=1200, raw_middle_total=2469,
    )
    session._audit_events.emit(
        "llm_request", model="x", input_chars=100,
        max_input_tokens_applied=8000, upstream_recovery_call_count=12,
    )
    _refresh(app)
    await pilot.pause()

    # A mid-episode lifecycle marker frame -- the exact shape
    # `test_compaction_episode_marker_frame_absorbs_into_the_open_entry`
    # asserts absorbs into the SAME open entry rather than appending its
    # own row (the folding #6085 is asking about, made visible: this frame
    # arrives and the row count does not grow).
    app._ingest_frame(OutboxMessage(  # noqa: SLF001
        kind="system", text="[⟳ compacting 3 turns]",
        meta={
            "compaction_episode_marker": True,
            "compaction_episode_seq": session._read_compaction_episode_seq(),  # noqa: SLF001
        },
    ))
    await pilot.pause()

    # END: the controller stops with no new failure since the entry
    # started -- the entry settles SUCCESS in place (same row, terminal
    # state), matching `test_entry_settles_success_when_compaction_stops_
    # with_no_new_failure`.
    session._compaction_controller._compacting = False  # noqa: SLF001
    _refresh(app)
    await pilot.pause()

    # Context AFTER -- another ordinary exchange, so the settled compaction
    # row is visibly sandwiched between chat rows on both sides in the
    # captured frame.
    await transport.push(
        OutboxMessage(kind="agent", text="done -- picking the refactor back up now", meta={"chain_id": "c1"})
    )
    await pilot.pause()
    await transport.push(OutboxMessage(kind="user", text="great, continue"))
    await pilot.pause()
