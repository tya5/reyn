"""Phase 5 TUI-rebuild gates (#3273): restore-on-restart hydration.

On ``reyn chat`` restart the Textual conversation pane should show the PREVIOUS
conversation (Claude-Code ``--resume`` parity) rather than starting blank. The
app hydrates its retained ``FlowModel`` from the persisted ``ChatMessage`` log
(``history.jsonl``) BEFORE the live frame pump starts, projecting each turn
through the SAME presenter/gutter path a live frame uses.

Graded invariants (mapped to the PR test plan):

1. **Restart shows the previous conversation** — a fixture ``ChatMessage`` log
   hydrates the model in order, resolved (never RUNNING), and those turns sit
   ABOVE the first live frame.
2. **Source = history.jsonl, not audit-events** — the LOCAL accessor reads the
   durable ``ChatMessage`` log loaded from ``history.jsonl`` (re-read from disk),
   never the P6 audit-event log.
3. **Non-authoritative projection / truncate-falsify N/A** — the ``FlowModel`` is
   a projection of the AUTHORITATIVE ``history.jsonl`` store, read DIRECTLY (not
   WAL-event-reconstructed), so the CLAUDE.md recovery truncate-falsify gate does
   not apply (there is no WAL-derived state to lose). See
   ``test_restore_source_is_authoritative_chat_log_gate_na``'s docstring.
4. **Retention = resume-equivalent** — the accessor restores whatever is in the
   log (a ``limit`` keeps the most-recent N; turns not in the log are absent).
5. **Import isolation + plain fallback preserved** — the accessor addition pulls
   no textual import into an always-loaded module; the subprocess witness re-runs.

All app-level tests use real instances (a concrete ``ClientTransport`` +
``ChatReadModel`` seam impl + the real app/pilot) and the accessor test uses a
real ``AgentRegistry`` + real ``Session`` — no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat._meta_keys import (
    RESULT_KIND_KEY,
    RESULT_META_KEY,
)
from reyn.interfaces.inline.textual_chat.restore import (
    RESTORED_META_KEY,
    project_restored_frames,
)
from reyn.interfaces.repl.read_model import ChatReadModel, RegistryReadModel, RemoteReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ── real seam impls (no mocks) ────────────────────────────────────────────────


class _HistoryReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (like ``RegistryReadModel``)
    returning a fixed persisted ``ChatMessage`` log — the app hydrates its model
    off ``conversation_history`` exactly as it would off the local registry."""

    def __init__(self, messages: "list[ChatMessage]") -> None:
        self._messages = list(messages)

    def snapshot(self, config=None):
        return None

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
        return Path("/tmp/reyn_phase5_input_history")

    def conversation_history(self, *, limit=None):
        return self._messages[-limit:] if limit is not None else list(self._messages)


class ScriptedTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport`. ``end=False`` keeps the stream
    open so the app stays mounted; ``messages`` are the LIVE frames pumped AFTER
    the mount-time hydration."""

    def __init__(self, messages: "list[OutboxMessage] | None" = None) -> None:
        self._messages = list(messages or [])
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _entries(app: TextualChatApp):
    return list(app.query_one(FlowView).entries)


# A fixture conversation log: a user turn, an assistant reply, a CORRELATED tool
# call+result (a real tool name + real non-empty args, per the round-trip
# non-default-value requirement), and a trailing turn — plus a ``system`` and a
# ``summary`` entry that must NOT surface (internal chrome, filtered at the LLM
# wire boundary). The tool-calling assistant turn carries only ``tool_calls``
# (empty text) — matching how the LLM actually emits a call — so it contributes
# no standalone ``agent`` frame; the call header reaches the display entirely
# through the coalesced ``tool`` projection.
def _fixture_log() -> "list[ChatMessage]":
    return [
        ChatMessage(role="user", content="first question"),
        ChatMessage(role="assistant", content="first answer"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "file__read",
                        "arguments": '{"path": "foo.py", "limit": 42}',
                    },
                }
            ],
        ),
        ChatMessage(
            role="tool", content="Read 42 lines", name="file__read",
            tool_call_id="call_1",
        ),
        ChatMessage(role="summary", content="(compactor carry — never shown)"),
        ChatMessage(role="user", content="second question"),
        ChatMessage(role="assistant", content="second answer"),
    ]


# ── Tier 1: pure projection (source-naming + ordering) ────────────────────────


def test_projection_maps_roles_preserves_order_and_names_source() -> None:
    """Tier 1: ``project_restored_frames`` maps user→user, assistant→agent, and a
    correlated tool call+result → a SINGLE coalesced ``tool_call_started`` frame
    (call header + folded result, matching the live-coalesce shape — no orphan
    ``tool_call_completed`` row), preserving conversation order and skipping the
    internal ``system``/``summary`` roles plus the tool-calling assistant turn's
    own (empty-text) row. Its input is the ``ChatMessage`` log (NOT
    audit-events) — the projection consumes ``ChatMessage`` values only."""
    frames = project_restored_frames(_fixture_log())
    # Leading resume divider (system) then the conversation, summary dropped,
    # and the tool-calling assistant turn contributes no standalone row.
    kinds = [f.kind for f in frames]
    assert kinds == [
        "system",  # ⤺ resume divider
        "user", "agent", "tool_call_started", "user", "agent",
    ], f"unexpected projection kinds: {kinds}"
    # No orphan tool_call_completed frame anywhere in the projection.
    assert "tool_call_completed" not in kinds

    # Order + text preserved for the conversational rows.
    convo = [(f.kind, f.text) for f in frames if f.kind != "system"]
    assert convo == [
        ("user", "first question"),
        ("agent", "first answer"),
        ("tool_call_started", "file__read"),
        ("user", "second question"),
        ("agent", "second answer"),
    ]
    # The coalesced tool row carries BOTH the correlated call (a REAL non-default
    # tool name + non-empty args, correlated by tool_call_id) AND the result.
    tool = next(f for f in frames if f.kind == "tool_call_started")
    assert tool.meta.get("tool") == "file__read"
    assert tool.meta.get("args") == {"path": "foo.py", "limit": 42}
    assert tool.meta.get(RESULT_KIND_KEY) == "tool_call_completed"
    assert tool.meta.get(RESULT_META_KEY) == {"result": "Read 42 lines"}
    # Every restored frame is marked as such.
    assert all(f.meta.get(RESTORED_META_KEY) is True for f in frames)


def test_projection_uncorrelated_tool_result_still_yields_header() -> None:
    """Tier 1: non-vacuity — a tool result with NO matching ``tool_call_id`` (e.g.
    truncated history: the correlating assistant turn is gone) still projects to
    the coalesced shape with the tool NAME as header (empty args), never an
    orphan ``⎿``-only row (``tool_call_completed`` with no call)."""
    frames = project_restored_frames([
        ChatMessage(
            role="tool", content="orphaned result", name="shell__run",
            tool_call_id="missing_call",
        ),
    ])
    tool_frames = [f for f in frames if f.kind != "system"]
    assert [f.kind for f in tool_frames] == ["tool_call_started"], (
        "an uncorrelated tool result must still project to exactly one "
        f"coalesced tool_call_started frame, got kinds={[f.kind for f in tool_frames]}"
    )
    tool = tool_frames[0]
    assert tool.meta.get("tool") == "shell__run"
    assert tool.meta.get("args") == {}
    assert tool.meta.get(RESULT_KIND_KEY) == "tool_call_completed"
    assert tool.meta.get(RESULT_META_KEY) == {"result": "orphaned result"}


def test_projection_dispatch_envelope_failure_classified_as_tool_call_failed() -> None:
    """Tier 1: a FAILED structured tool result (#73) — the persisted ``m.text``
    for a dispatch-envelope error (e.g. ``file__read`` on a missing path) is
    NEVER JSON; it is the plain string ``"Error (<kind>): <message>"`` produced
    by the tool-result renderer (``reyn.core.offload.seam`` / ``router_loop.py``
    — grounded by direct inspection, not guessed). The projection must classify
    this as ``RESULT_KIND_KEY="tool_call_failed"`` (not ``"tool_call_completed"``)
    so the presenter's failure branch (``_tool_result_line`` /
    ``_body_and_background`` in ``presenter.py``) tints the row coral, exactly
    matching the LIVE rendering of the same failure.

    Non-vacuity: the OLD code passed ``{"result": m.text}`` (the raw string)
    through unconditionally — ``summarize_tool_result`` only recognises
    failure on a DICT carrying an ``error``/``error_message`` key
    (``renderer.py::_summarize_result``), so handing it a bare string can
    never produce a ``✗``-prefixed summary. That is asserted directly below."""
    from reyn.interfaces.repl.renderer import summarize_tool_result

    failure_text = "Error (not_found): file not found: missing.py"
    frames = project_restored_frames([
        ChatMessage(
            role="tool", content=failure_text, name="file__read",
            tool_call_id="missing_call",
        ),
    ])
    tool = next(f for f in frames if f.kind == "tool_call_started")
    assert tool.meta.get(RESULT_KIND_KEY) == "tool_call_failed", (
        f"a rendered tool failure must classify as tool_call_failed, got "
        f"meta={tool.meta}"
    )
    result_meta = tool.meta.get(RESULT_META_KEY)
    assert result_meta == {
        "error_kind": "not_found",
        "error_message": "file not found: missing.py",
    }

    # Non-vacuity: the OLD behaviour (raw string fed to summarize_tool_result)
    # never detects a failure — proving the fix actually changed the outcome.
    old_summary = summarize_tool_result("file__read", failure_text)
    assert not old_summary.startswith("✗"), (
        "sanity check failed: the OLD string-based summary already looked "
        "like a failure, so this test would not prove the fix changed anything"
    )


def test_projection_mcp_iserror_failure_classified_as_tool_call_failed() -> None:
    """Tier 1: the second (and only other) failure shape the renderer emits —
    an MCP ``isError`` result — persists as the plain string
    ``"Error: <text>"`` (no ``(<kind>)`` segment). The projection must still
    classify this as ``tool_call_failed``, carrying just ``error_message``."""
    frames = project_restored_frames([
        ChatMessage(
            role="tool", content="Error: connection refused", name="mcp__ping",
            tool_call_id="missing_call",
        ),
    ])
    tool = next(f for f in frames if f.kind == "tool_call_started")
    assert tool.meta.get(RESULT_KIND_KEY) == "tool_call_failed"
    assert tool.meta.get(RESULT_META_KEY) == {"error_message": "connection refused"}


def test_projection_success_mentioning_error_word_not_misclassified() -> None:
    """Tier 1: no false positive — a SUCCESSFUL tool result whose own body
    happens to contain the substring "error" (e.g. a grep result) must NOT be
    misclassified as a failure. Only the renderer's exact failure PREFIXES
    (``"Error ("`` / ``"Error: "``) trigger ``tool_call_failed``; a mid-body
    mention does not (this is the substring-heuristic the brief explicitly
    forbids inventing)."""
    benign_text = "3 matches for 'error handling' in src/foo.py"
    frames = project_restored_frames([
        ChatMessage(
            role="tool", content=benign_text, name="grep__search",
            tool_call_id="missing_call",
        ),
    ])
    tool = next(f for f in frames if f.kind == "tool_call_started")
    assert tool.meta.get(RESULT_KIND_KEY) == "tool_call_completed"
    assert tool.meta.get(RESULT_META_KEY) == {"result": benign_text}


def test_projection_empty_log_yields_no_frames_no_divider() -> None:
    """Tier 1: a first-ever run (empty log) projects to nothing — no divider, so
    the pane stays blank (resume-equivalent: nothing to restore)."""
    assert project_restored_frames([]) == []


def test_projection_skips_blank_text_turns() -> None:
    """Tier 1: a whitespace-only user/assistant turn is not projected (it would
    render an empty row); a tool result with no text still projects (its meta
    carries the summary)."""
    frames = project_restored_frames([
        ChatMessage(role="user", content="   "),
        ChatMessage(role="assistant", content=""),
    ])
    assert frames == []


# ── Tier 2: app hydration end-to-end ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_shows_previous_conversation_before_live_frames() -> None:
    """Tier 2: the CORE witness — mounting the app hydrates the retained model
    from the persisted ``ChatMessage`` log so the PREVIOUS conversation is present
    in order, and those restored turns sit ABOVE the first LIVE frame (hydration
    runs at ``on_mount`` before the frame pump)."""
    live = OutboxMessage(kind="user", text="LIVE turn after restart")
    app = TextualChatApp(
        transport=ScriptedTransport([live]),
        read_model=_HistoryReadModel(_fixture_log()),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entries = _entries(app)
        rows = [(e.item.kind, e.item.text) for e in entries]
        # Restored conversation (divider + turns) precedes the live frame.
        assert rows == [
            ("system", "⤺ resumed previous conversation"),
            ("user", "first question"),
            ("agent", "first answer"),
            ("tool_call_started", "file__read"),
            ("user", "second question"),
            ("agent", "second answer"),
            ("user", "LIVE turn after restart"),
        ], f"restore/live ordering wrong: {rows}"
        # The live frame is LAST — restore happened before the pump.
        assert rows[-1] == ("user", "LIVE turn after restart")


@pytest.mark.asyncio
async def test_restored_entries_render_resolved_never_running() -> None:
    """Tier 2: every restored entry is RESOLVED, never RUNNING — a restored tool
    result carries SUCCESS (green gutter), user/agent rows keep DEFAULT. A restart
    must never leave a phantom RUNNING (blinking) row for a settled past turn."""
    app = TextualChatApp(
        transport=ScriptedTransport(),
        read_model=_HistoryReadModel(_fixture_log()),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entries = _entries(app)
        assert entries, "no entries were hydrated"
        assert all(e.state is not EntryState.RUNNING for e in entries), (
            "a restored turn is RUNNING — settled past turns must be resolved"
        )
        tool = next(e for e in entries if e.item.kind == "tool_call_started")
        assert tool.state is EntryState.SUCCESS, "restored tool result not resolved to SUCCESS"


@pytest.mark.asyncio
async def test_restored_failed_tool_resolves_to_error_state() -> None:
    """Tier 2: end-to-end witness for #73 — a persisted FAILED structured tool
    result (``"Error (not_found): ..."``) hydrates to ``EntryState.ERROR`` (the
    same coral-tinted terminal state the LIVE completion handler assigns a
    raised/reported failure), not ``SUCCESS``. ``app.py``'s
    ``_hydrate_from_history`` already branches on
    ``meta[RESULT_KIND_KEY] == "tool_call_failed"`` to set ``ERROR`` directly
    (verified by reading the source — no app.py change was needed for this
    fix); this test proves the WHOLE path (projection → hydration) delivers
    the correct end state, not just the projection in isolation."""
    log = [
        ChatMessage(
            role="tool", content="Error (not_found): file not found: missing.py",
            name="file__read", tool_call_id="missing_call",
        ),
    ]
    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_HistoryReadModel(log)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entries = _entries(app)
        tool = next(e for e in entries if e.item.kind == "tool_call_started")
        assert tool.state is EntryState.ERROR, (
            f"a restored FAILED tool must resolve to ERROR, got {tool.state}"
        )


@pytest.mark.asyncio
async def test_remote_read_model_hydrates_nothing_graceful_degrade() -> None:
    """Tier 2: a read model that cannot serve past turns (remote frame-sufficiency
    boundary → empty log) hydrates nothing — the app mounts with a blank pane and
    no fabricated turn, exactly as before Phase 5."""
    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_HistoryReadModel([])
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert _entries(app) == [], "an empty log must hydrate zero entries (no divider)"


def test_remote_read_model_conversation_history_is_empty() -> None:
    """Tier 2: the REAL ``RemoteReadModel`` returns an empty conversation log —
    the past-turn ``ChatMessage`` log is session-local and NOT on the AG-UI wire,
    so a remote client degrades gracefully (never a fabricated turn)."""
    rm = RemoteReadModel(ScriptedTransport())
    assert rm.conversation_history() == []
    assert rm.conversation_history(limit=5) == []


# ── Tier 2: LOCAL accessor source + retention (real registry + Session) ───────


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        # Production parity (chat.py): rehydrate the persisted conversation from
        # history.jsonl at build time.
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("solo")
    return reg


@pytest.mark.asyncio
async def test_restore_source_is_authoritative_chat_log_gate_na(tmp_path, monkeypatch) -> None:
    """Tier 2: the LOCAL accessor restores from the durable ``ChatMessage`` log on
    disk (``history.jsonl``), NOT the P6 audit-event log — turns appended and
    persisted survive a drop of the in-memory list + a fresh ``load_history``.

    Truncate-falsify recovery gate — DETERMINATION: N/A. The CLAUDE.md
    recovery-feature gate guards WAL-event-DERIVED reconstruction state (state a
    truncation of ``wal.jsonl`` below its source events would silently lose).
    Restore here is NOT that: ``history.jsonl`` is the AUTHORITATIVE conversation
    store and the accessor reads it DIRECTLY (derived-at-read, not
    WAL-reconstructed), so there is no WAL-derived state to lose and no truncate-
    falsify obligation. This test witnesses the authoritative-disk source; were a
    future change to make any restored surface WAL-reconstructed, the gate WOULD
    apply and this determination must be revisited."""
    monkeypatch.chdir(tmp_path)  # isolate the session's cwd-relative workspace/history.jsonl
    reg = _registry(tmp_path)
    try:
        session = await reg.attach("solo")
        session._append_history(ChatMessage(role="user", content="restore me please"))
        session._append_history(ChatMessage(role="assistant", content="here is your restore"))
        # Prove the DISK log (history.jsonl) is the source: drop the in-memory
        # list and re-read from disk. If restore rode audit-events (which do not
        # carry assistant text), the reply text would be gone.
        session.history.clear()
        session.load_history()

        rm = RegistryReadModel(reg)
        restored = rm.conversation_history()
        assert [(m.role, m.text) for m in restored] == [
            ("user", "restore me please"),
            ("assistant", "here is your restore"),
        ], "accessor did not return the disk-persisted ChatMessage log"
        assert session.history_path.name == "history.jsonl"
        assert session.history_path.exists(), "the source file is history.jsonl on disk"
    finally:
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


@pytest.mark.asyncio
async def test_retention_is_resume_equivalent(tmp_path, monkeypatch) -> None:
    """Tier 2: retention is resume-equivalent — the accessor restores whatever is
    in the log (N turns), and a ``limit`` keeps only the most-recent N (a caller's
    cross-session view need not restore the whole log). Nothing outside the log is
    ever returned."""
    monkeypatch.chdir(tmp_path)  # isolate the session's cwd-relative workspace/history.jsonl
    reg = _registry(tmp_path)
    try:
        session = await reg.attach("solo")
        for i in range(5):
            session._append_history(ChatMessage(role="user", content=f"turn-{i}"))
        rm = RegistryReadModel(reg)

        full = rm.conversation_history()
        assert [m.text for m in full] == [f"turn-{i}" for i in range(5)], (
            "full restore must return every persisted turn, in order"
        )
        recent = rm.conversation_history(limit=2)
        assert [m.text for m in recent] == ["turn-3", "turn-4"], (
            "limit must keep the most-recent N turns (resume-equivalent)"
        )
    finally:
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


# ── Tier 2c: import isolation preserved (accessor adds no always-loaded import) ─

_ISOLATION_SUBPROCESS = '''
import sys


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("textual", "textual_flowview"):
            raise ModuleNotFoundError("blocked for isolation test: " + name)
        return None


sys.meta_path.insert(0, _Block())

import reyn.interfaces.repl.client_driver  # noqa: E402,F401
import reyn.interfaces.repl.read_model  # noqa: E402,F401  (the accessor lives here — must stay textual-free)
import reyn.interfaces.repl.stream_client  # noqa: E402,F401
import reyn.interfaces.cli.commands.chat  # noqa: E402,F401
import reyn.runtime.chat_message  # noqa: E402,F401  (the accessor's return type)

assert "textual_flowview" not in sys.modules, "flowview imported at module load"
assert "textual" not in sys.modules, "textual imported at module load"
print("ISOLATION_OK")
'''


def test_phase5_accessor_imports_stay_tty_only() -> None:
    """Tier 2c: with ``textual`` / ``textual_flowview`` unimportable, the plain /
    non-TTY path — INCLUDING ``read_model`` (where the new ``conversation_history``
    accessor lives) and ``chat_message`` (its return type) — still imports green.
    The Phase-5 accessor added no top-level textual import to an always-loaded
    module. Runs the strip in a clean subprocess."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SUBPROCESS],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ISOLATION_OK" in proc.stdout, f"stdout={proc.stdout}\nstderr={proc.stderr}"
