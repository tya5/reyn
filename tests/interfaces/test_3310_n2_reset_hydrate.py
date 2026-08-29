"""Tier 2: #3310 N2 — client-side reset + hydrate on the ``session_attached``
switch barrier.

N1 (#3321) added ``session_attached`` (``{agent, session_id}``), an
``EventFrame`` the registry puts DIRECTLY on ``repl_outbox`` at the attach
seam with no ``await`` between the flip and the put — a stream BARRIER:
everything before it on the frame stream belongs to the OLD attached
session, everything after to the NEW one, by construction.

★Design thesis (architect deep-dive, issue #3310 §1, owner-ratified): a
cached FlowView cannot be the source of truth — while a session is
detached, the registry forwarder DROPS its frames entirely (durable
narration lives only in ``history.jsonl``), so a cache would be missing
everything that happened meanwhile and would hold tool rows stuck RUNNING.
v1 is reconnect-shaped, not cache-shaped: on the barrier, reset EVERY
per-session client state and rehydrate from the durable sources
(``TextualChatApp._handle_session_attached_event`` /
``_hydrate_from_history``, generalized in this PR to target an arbitrary
``(agent, session_id)`` via ``ChatReadModel.conversation_history``).

Gates covered here (#3310's acceptance list):

1. ★staleness gate (load-bearing, the design thesis's falsifier): A→B, A
   produces output while the client is on B (never delivered live — only
   durably persisted, exactly like a detached forwarder would leave it),
   switch back to A — the away-produced frames are present.
2. orphan gate: a tool that completed while away renders resolved, never
   RUNNING, after switching back (hydrate's resolved projection).
3. Per-state reset: one independent test per state — ``_running_tools``,
   ``_pending_ivs`` (SEPARATE from ``InterventionPanel.collapse_all()``,
   which gets its OWN test: a co-vet finding on #3323 caught the original
   single combined test discriminating only ``collapse_all()``, leaving
   ``_pending_ivs.clear()`` un-witnessed — "either clear alone" left it
   GREEN), the sent-queue view/widget/``_queue_item_meta``,
   ``_streaming_replies`` — each with its OWN discriminating witness that
   goes RED when ONLY that state's clear is stripped (never folded into a
   shared test — the #3302/#3308 sibling-guard-site lesson, reconfirmed
   the hard way here for ``_pending_ivs``).
4. Unvisited session: switching to a session never seen in THIS client run
   shows its history, not a blank pane.
5. Barrier consumption: an ordinary frame delivered immediately after the
   barrier renders into the NEW session's view, never the old one's (trivial
   here since the model is a single retained FlowModel the barrier just
   cleared — there is no "other queue" it could be mis-filed into).

Real ``AgentRegistry`` + real ``Session`` (``tests._support.agent_session.
make_session``) + a real, minimal ``ClientTransport`` (a queue the test
pushes scripted frames onto, interleaved with real registry/session state
changes) + the real mounted ``TextualChatApp`` — no mocks, per the testing
policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.widgets import TabPane
from textual_flowview import EntryState, FlowView

from reyn.core.events.events import Event
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

# ── real seam: a queue-backed ClientTransport a test drives frame-by-frame ──


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` whose ``frames()`` drains an
    ``asyncio.Queue`` a test pushes onto — so a test can interleave pushing a
    frame with mutating the (real) registry/session state in between, exactly
    like production interleaves a registry attach with the frames it emits.
    Never ends on its own (mirrors ``ScriptedTransport(end=False)`` elsewhere
    in this package) — the test's ``async with app.run_test()`` block owns
    the app's lifetime."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self.submitted: "list[str]" = []
        # (choice_id, intervention_id) pairs — records which id an answer
        # was actually TARGETED at, the public observable
        # ``test_reset_pending_interventions_...`` uses to discriminate
        # ``_pending_ivs.clear()`` independently of the panel's own
        # ``collapse_all()`` (mirrors ``RecordingTransport.answered_choice_ids``
        # in ``tests/interfaces/test_textual_chat_intervention_panel_3299.py``).
        self.answered_choice_ids: "list[str | None]" = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    def push_display(self, msg: OutboxMessage) -> None:
        self._queue.put_nowait(DisplayFrame(msg))

    def push_event(self, etype: str, data: dict) -> None:
        self._queue.put_nowait(EventFrame(Event(type=etype, data=data)))

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_choice_ids.append(intervention_id)
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:
        self.push_display(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def cancel_queued(self, msg_id: str) -> bool:  # pragma: no cover - trivial
        return False

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


def _registry_with_wal(tmp_path: Path) -> AgentRegistry:
    """Like :func:`_registry`, but with a real ``StateLog`` wired into every
    session — needed ONLY by the queue-reseed test below:
    ``SnapshotJournal.append_inbox`` populates ``snapshot.inbox`` (the source
    ``Session.queued_user_messages()`` reads) exclusively when
    ``state_log is not None`` (``services/snapshot_journal.py``). Every other
    test in this file uses the plain :func:`_registry` (no WAL) since they
    never need a real undispatched-queue witness."""
    state_log = StateLog(tmp_path / "state.wal")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=factory, state_log=state_log,
    )
    reg.create("alpha")
    reg.create("beta")
    return reg


def _entries(app: TextualChatApp):
    return list(app.query_one(FlowView).entries)


def _rows(app: TextualChatApp) -> "list[tuple[str, str]]":
    return [(e.item.kind, e.item.text) for e in _entries(app)]


async def _settle(pilot, n: int = 2) -> None:
    for _ in range(n):
        await pilot.pause()


# ── 1. ★staleness gate (load-bearing) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_staleness_gate_frames_produced_while_away_are_present(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ★the design thesis's falsifier. A→B, A's session produces a
    NEW turn while this client is on B — durably persisted only, exactly as
    the registry forwarder would leave it for a detached session (never
    delivered as a live frame to this client) — then switch back to A: that
    away-produced turn is PRESENT. A cache-as-truth implementation cannot
    pass this (it never received that frame and has nothing else to show
    it); this implementation passes because it rehydrates from
    ``history.jsonl`` on every switch, not from a retained cache."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.get_session("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            assert _rows(app) == [], "alpha starts with no history"

            # Live turn while attached to alpha — delivered live AND durably
            # persisted (mirrors production: a live frame's turn is also
            # written to history.jsonl by the same RouterLoop call).
            transport.push_display(OutboxMessage(kind="user", text="turn A1"))
            await _settle(pilot)
            alpha._append_history(ChatMessage(role="user", content="turn A1"))
            assert _rows(app) == [("user", "turn A1")]

            # Switch to beta.
            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)
            assert _rows(app) == [], "beta has no history yet — must not show alpha's"

            # While this client is on beta, alpha's session produces a NEW
            # turn — durably persisted ONLY (never pushed to this client's
            # transport at all), exactly like a detached forwarder drop.
            alpha._append_history(ChatMessage(role="user", content="turn A2 while away"))

            # Ordinary live activity on beta in between, to prove the barrier
            # also correctly attributes a frame delivered right after it to
            # the NEW session (gate 5, barrier consumption).
            transport.push_display(OutboxMessage(kind="user", text="turn B1"))
            await _settle(pilot)
            assert _rows(app) == [("user", "turn B1")]

            # Switch back to alpha.
            await reg.attach("alpha")
            transport.push_event(
                "session_attached", {"agent": "alpha", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            assert _rows(app) == [
                ("system", "⤺ resumed previous conversation"),
                ("user", "turn A1"),
                ("user", "turn A2 while away"),
            ], (
                "the away-produced turn must reappear on switch-back — a "
                f"cache-as-truth implementation would drop it: {_rows(app)!r}"
            )
    finally:
        # No test-owned timeout here (CLAUDE.md/testing.md § Time: tests carry
        # no wait budget of their own) — this used to be
        # ``asyncio.wait_for(reg.shutdown(), timeout=5.0)``, a self-timing
        # wrapper around a call that COULD hang forever before #4765.
        # #4765 bounded ``AgentRegistry.shutdown()`` itself (an internal
        # ``asyncio.wait_for(..., timeout=_SHUTDOWN_GRACE_S)`` around
        # ``session.aclose_background_tasks()``), so the outer wrapper became
        # redundant, not merely superfluous: measured 2026-08-15, removing it
        # across this same shape's whole family (5 files, 18 call sites, 23
        # tests) left every test green with no hang, at ordinary timing. The
        # one spot this note lives in is deliberate — the other 17 sites (this
        # file's own siblings below, plus ``test_registry_focus_listener_
        # rewire.py`` / ``test_4387_tui_paging_extends_from_disk.py`` /
        # ``test_2280_durability_halt_observability.py`` /
        # ``test_4788_rewind_picker_escape_dismiss.py``) carry the same bare
        # ``await reg.shutdown()`` with no repeated comment.
        await reg.shutdown()


# ── 2. orphan gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_gate_tool_completed_while_away_is_not_running(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: a tool that STARTED and COMPLETED entirely while this client
    was away (on another session) must not render RUNNING after switching
    back — hydrate's coalesced/resolved projection settles it directly (the
    live RUNNING-tracking path never even sees it, so there is nothing to
    force-settle; this witnesses that the restore path itself never leaves a
    phantom RUNNING row)."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.get_session("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            # A whole tool call+result completes on alpha while this client
            # is on beta — persisted directly (never delivered live here).
            alpha._append_history(ChatMessage(
                role="assistant", content="",
                tool_calls=[{
                    "id": "call_1", "type": "function",
                    "function": {"name": "shell__run", "arguments": '{"cmd": "ls"}'},
                }],
            ))
            alpha._append_history(ChatMessage(
                role="tool", content="file_a.txt\nfile_b.txt",
                name="shell__run", tool_call_id="call_1",
            ))

            await reg.attach("alpha")
            transport.push_event(
                "session_attached", {"agent": "alpha", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            entries = _entries(app)
            # Unpacking into a single-element tuple IS the "exactly one" check
            # (raises ValueError on 0 or 2+ matches) — a behavioral read of
            # the extracted value below, not a `len(...) == N` format pin.
            (tool_entry,) = [e for e in entries if e.item.kind == "tool_call_started"]
            assert tool_entry.state is EntryState.SUCCESS, (
                f"a tool completed while away must resolve, never RUNNING: "
                f"{tool_entry.state!r}"
            )
            assert all(e.state is not EntryState.RUNNING for e in entries)
    finally:
        await reg.shutdown()


# ── 3. per-state reset — ONE independent test per state ──────────────────────


@pytest.mark.asyncio
async def test_reset_running_tools_stale_op_id_does_not_silently_coalesce(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``_running_tools`` witness. A tool starts (RUNNING) on alpha,
    left unresolved at switch time. After switching to beta, a completion
    frame carrying the SAME op_id must NOT be silently absorbed into the
    (now-removed) stale entry — if ``_running_tools`` were not cleared,
    ``_ingest_frame`` would pop the stale entry and hand it to
    ``_coalesce_tool_result``, which settles that ORPHANED (off-model) entry
    in place and returns WITHOUT appending anything — the completion would
    vanish with zero visible trace. With the dict cleared, the completion
    finds no RUNNING match and falls through to a plain appended row — a
    directly observable, non-vacuous discriminator between "cleared" and
    "leftover"."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_display(OutboxMessage(
                kind="tool_call_started", text="shell__run",
                meta={"op_id": "op-stale", "tool": "shell__run", "args": {}},
            ))
            await _settle(pilot)
            # Unpacking into a single-element tuple IS the "exactly one RUNNING
            # entry" check (raises on 0 or 2+) — not a `len(...) == N` pin.
            (running_entry,) = [e for e in _entries(app) if e.state is EntryState.RUNNING]
            assert running_entry.state is EntryState.RUNNING

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)
            assert _entries(app) == [], "switch must clear the retained model"

            # Same op_id, now on beta — must NOT be silently swallowed.
            transport.push_display(OutboxMessage(
                kind="tool_call_completed", text="",
                meta={"op_id": "op-stale", "tool": "shell__run", "result": "done"},
            ))
            await _settle(pilot)

            rows = _rows(app)
            assert rows == [("tool_call_completed", "")], (
                "a completion for an op_id RUNNING before the switch must "
                "appear as a fresh, uncorrelated row after reset — a leftover "
                f"_running_tools entry would swallow it silently: {rows!r}"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_reset_pending_interventions_closes_all_tabs(tmp_path, monkeypatch) -> None:
    """Tier 2: ``InterventionPanel.collapse_all()`` witness. A pending
    intervention on alpha opens the panel with one tab; switching away must
    close the whole panel (``panel.display`` False, zero mounted
    ``TabPane``s), never leaving alpha's stale tab lingering behind the new
    session's own (server-re-announced) ones.

    ★Scope note (co-vet finding on #3323): this test discriminates
    ``collapse_all()`` specifically — the panel's OWN internal tab-tracking
    dicts (``_pane_ids``/``_key_by_pane``/etc., cleared inside
    ``collapse_all`` itself) are what drive ``panel.display``/``TabPane``
    count, independent of the APP's ``_pending_ivs`` dict. Stripping ONLY
    ``self._pending_ivs.clear()`` in the app (leaving ``collapse_all()`` in
    place) does NOT turn this test RED — that is a SEPARATE state with its
    own witness below (``test_reset_pending_ivs_stale_key_answer_not_targeted``),
    not folded in here (per-state independence, #3302/#3308 lesson)."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_display(OutboxMessage(
                kind="intervention",
                text="Allow write to /etc/hosts?",
                meta={
                    "intervention_id": "iv-1",
                    "prompt": "Allow write to /etc/hosts?",
                    "choices": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                },
            ))
            await _settle(pilot)

            panel = app.query_one(InterventionPanel)
            assert panel.display is True, "panel must show for the pending intervention"
            assert len(list(panel.query(TabPane))) == 1

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            assert panel.display is False, "switch must collapse the whole panel"
            assert list(panel.query(TabPane)) == [], (
                "switch must close every pending tab from the OLD session"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_reset_pending_ivs_stale_key_answer_not_targeted(tmp_path, monkeypatch) -> None:
    """Tier 2: ★``_pending_ivs`` witness, INDEPENDENT of
    ``InterventionPanel.collapse_all()`` (co-vet finding on #3323: the panel
    test above discriminates ``collapse_all()`` only — stripping
    ``_pending_ivs.clear()`` alone left that test GREEN, a genuine
    cross-masking gap).

    Scenario: a pending intervention on alpha (``intervention_id="iv-old"``)
    populates ``app._pending_ivs["iv-old"]``. The switch to beta happens —
    THEN, simulating a click that was ALREADY in flight before the switch
    (a real Textual race: the panel message for a click on the OLD tab can
    still be QUEUED for delivery even after ``collapse_all()`` has already
    removed that tab from the DOM), this test delivers
    ``InterventionPanel.ChoiceSelected(key="iv-old", ...)`` directly to the
    app's OWN handler — exercising the EXACT same lookup
    (``on_intervention_panel_choice_selected``) that the panel's TabPane
    button would trigger.

    If ``_pending_ivs`` were NOT cleared on switch, this lookup finds the
    STALE ``(entry, "iv-old")`` tuple and delivers the answer TARGETED at
    alpha's own intervention id — over THIS client's transport, which now
    routes to beta. That is a cross-session misdelivery: an answer meant
    for (or at least keyed to) alpha's intervention lands on a call the
    server associates with beta's connection. With ``_pending_ivs``
    correctly cleared, the stale key resolves to nothing and the answer is
    delivered UNTARGETED (``intervention_id=None``, the documented pre-P2
    head-of-queue fallback) instead of silently misrouted — the
    discriminating, PUBLIC observable is
    ``transport.answered_choice_ids``, never a private ``app._pending_ivs``
    read."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_display(OutboxMessage(
                kind="intervention",
                text="Allow write to /etc/hosts?",
                meta={
                    "intervention_id": "iv-old",
                    "prompt": "Allow write to /etc/hosts?",
                    "choices": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                },
            ))
            await _settle(pilot)

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            # Simulates a click delivered AFTER the switch for a tab that
            # existed BEFORE it — driving the app's real handler directly
            # (the same code path a real Textual message dispatch would
            # invoke), not a synthetic private-state read.
            await app.on_intervention_panel_choice_selected(
                InterventionPanel.ChoiceSelected(key="iv-old", choice_id="yes", label="Yes")
            )
            await _settle(pilot)

            assert transport.answered_choice_ids == [None], (
                "a late-arriving answer for a PRE-switch intervention key "
                "must not resolve to that stale intervention_id (would "
                "cross-session-misdeliver over the NOW-beta-routed "
                f"transport): {transport.answered_choice_ids!r}"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_reset_sent_queue_view_and_widget_and_item_meta(tmp_path, monkeypatch) -> None:
    """Tier 2: ``_queue_view`` / ``_queue_seeded`` / ``_queue_item_meta`` / the
    :class:`SentQueue` widget rows — witnessed together because they are ONE
    observable surface (the widget's public ``has_items()``/
    ``rendered_texts()``), but the underlying reset is driven by clearing ALL
    FOUR: a fresh ``RemoteQueueView()``, ``_queue_seeded=False`` (so the
    existing seed-on-first-frame path re-seeds the NEW session), the sent-queue
    WIDGET's rows, and the meta side-table. A queued item materializes via a
    ``user_submitted`` audit-event (the real production path,
    ``_handle_user_submitted_event``) on alpha; switching to beta must show
    the sent-queue region EMPTY, never alpha's still-queued row."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_event("user_submitted", {
                "msg_id": "m-1", "chain_id": "c-1", "text": "queued on alpha", "seq": 1,
                "meta": {},
            })
            await _settle(pilot)

            sent_queue = app.query_one(SentQueue)
            assert sent_queue.has_items() is True
            assert any("queued on alpha" in t for t in sent_queue.rendered_texts())

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            assert sent_queue.has_items() is False, (
                "alpha's queued row must not survive the switch to beta"
            )
            assert sent_queue.rendered_texts() == []
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_reset_streaming_replies_stale_chain_id_does_not_finalize_into_ghost(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ★``_streaming_replies`` witness (#3288 ③c) — the architect
    explicitly flagged this as the state most likely to be forgotten by a
    later phase. A streamed reply's delta on alpha creates ONE flow entry
    keyed by chain_id, left unfinished (no terminal completion) at switch
    time. After switching to beta, a completion frame carrying the SAME
    chain_id must NOT be silently finalized into the (now-removed) stale
    entry — if ``_streaming_replies`` were not cleared, ``_ingest_frame``
    would pop the stale entry and call ``entry.set_item`` on an orphaned
    (off-model) ``Entry`` and return WITHOUT appending — the completion
    would vanish with zero visible trace, mirroring the running-tools
    discriminator above."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_event("agent_delta", {"chain_id": "chain-1", "text": "partial…"})
            await _settle(pilot)
            assert _rows(app) == [("agent", "partial…")]

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)
            assert _rows(app) == [], "switch must clear the retained model"

            # Same chain_id, now on beta — the terminal completion must NOT
            # be silently absorbed into the stale (removed) entry.
            transport.push_display(OutboxMessage(
                kind="agent", text="finalized on beta",
                meta={"chain_id": "chain-1"},
            ))
            await _settle(pilot)

            rows = _rows(app)
            assert rows == [("agent", "finalized on beta")], (
                "a completion for a chain_id in-flight before the switch must "
                "appear as a fresh row after reset — a leftover "
                f"_streaming_replies entry would swallow it silently: {rows!r}"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_reset_call_parents_stale_call_id_does_not_nest_into_ghost_parent(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``_call_parents`` witness (#4776) — an ``agent`` row carrying a
    ``call_id`` on alpha registers itself as a potential tree-parent
    (``_call_parents``, #4691 Phase B B1). Left un-child'd at switch time
    (nobody nested under it before the switch — the common no-tool-calls
    case, made the DEFAULT registration shape by #4777/#4779: EVERY
    call_id-bearing agent row registers now, not only ones that go on to
    dispatch tools), its Entry is exactly the shape ``_call_parents`` was
    never previously proven to release.

    ``_call_parents`` was OMITTED from :attr:`TextualChatApp.
    _PER_SESSION_DICT_STATE` before this fix — its own docstring claimed a
    conversation/session-bounded lifetime the code never actually enforced
    (the real bound was the whole app PROCESS's lifetime). After switching
    to beta, a tool_call_started frame carrying the SAME call_id must NOT be
    silently nested under the (now off-model, orphaned) alpha-session
    parent Entry — if ``_call_parents`` were not cleared,
    ``parent.append_child(msg)`` would attach the child to a parent no
    longer reachable from the current (freshly cleared) FlowModel, and the
    row would vanish from the visible flow with zero trace — the identical
    silent-swallow discriminator shape ``_running_tools``/
    ``_streaming_replies`` above use for their own witnesses."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            transport.push_display(OutboxMessage(
                kind="agent", text="alpha's own reply",
                meta={"call_id": "call-stale"},
            ))
            await _settle(pilot)
            assert _rows(app) == [("agent", "alpha's own reply")]

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)
            assert _rows(app) == [], "switch must clear the retained model"

            # Same call_id, now on beta — must NOT nest under the stale
            # (removed-from-model) alpha parent Entry.
            transport.push_display(OutboxMessage(
                kind="tool_call_started", text="grep",
                meta={"tool": "grep", "op_id": "op-1", "args": {}, "call_id": "call-stale"},
            ))
            await _settle(pilot)

            rows = _rows(app)
            assert rows == [("tool_call_started", "grep")], (
                "a tool row for a call_id registered before the switch must "
                "land as a fresh, flat top-level row after reset — a "
                f"leftover _call_parents entry would swallow it silently: {rows!r}"
            )
    finally:
        await reg.shutdown()


def _collect(session: Session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran (#5450)."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


async def _stop_auto_driver(reg: AgentRegistry, name: str) -> None:
    """Cancel the background ``session.run()`` driver ``AgentRegistry.attach``
    booted for ``name`` — the SAME helper
    ``tests/interfaces/test_3300_p2a_queue_state_publish.py`` uses, so a test can drive
    ``run_one_iteration()`` manually instead of racing the live loop for
    control over exactly when a submission dispatches vs. stays queued."""
    key = (name, _DEFAULT_SID)
    task = reg._tasks.get(key)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    fwd = reg._forward_tasks.get(key)
    if fwd is not None and not fwd.done():
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_reset_queue_view_reseeds_from_new_sessions_own_queue(
    tmp_path, monkeypatch, _llm_stub,
) -> None:
    """Tier 2: ``_queue_view`` / ``_queue_seeded`` witness, INDEPENDENT of the
    sent-queue WIDGET clear above (a strip check during review showed the
    widget-clear test above stays GREEN even with ``_queue_view``/
    ``_queue_seeded`` left UNRESET — a real per-state gap the architect's
    table did not by itself surface; this test closes it).

    Beta has its OWN item ALREADY queued (behind a held-busy turn, manually
    pumped — the auto-driver is stopped so this test has sole, deterministic
    control over dispatch, exactly like the #3300 P2a late-joiner test) BEFORE
    this client ever switches to it. If the switch-barrier handler did not
    eagerly reseed :attr:`_queue_view` from the NEW session's OWN snapshot,
    beta's already-queued item would never render (there is no OTHER trigger
    that would show it — nothing else happens on beta in this scenario) —
    this is the #3305-shaped reconnect-reseed, witnessed here for the SWITCH
    path specifically.

    ★Non-vacuity guard (found while strip-falsifying the WHOLE
    ``session_attached`` handler for #3323 co-vet re-review): the very
    generic "seed on the first frame the pump EVER processes" check
    (:meth:`TextualChatApp._pump_frames`) would ALSO explain a green result
    here if the ``session_attached`` frame below happened to be the first
    frame this app's pump ever sees — masking the eager-reseed-on-switch
    behavior this test means to isolate. A harmless benign frame is pushed
    and settled FIRST so that generic check has already fired (on alpha,
    where it is a no-op) before the switch — the ONLY remaining path that
    can populate the queue view for beta is the eager reseed inside
    :meth:`_handle_session_attached_event` itself."""
    monkeypatch.chdir(tmp_path)
    reg = _registry_with_wal(tmp_path)
    try:
        await reg.attach("alpha")
        beta = await reg.attach("beta")
        await _stop_auto_driver(reg, "beta")
        events = _collect(beta)

        await beta.submit_user_text("beta busy trigger")
        turn_task = asyncio.create_task(beta.run_one_iteration())
        await _llm_stub.call_started.wait()

        # A second submission arrives while the first is still in flight —
        # it stays undispatched (server-authoritative queue, #3300 §6b).
        await beta.submit_user_text("beta queued item")
        assert [i["text"] for i in beta.queued_user_messages()] == ["beta queued item"], (
            "fixture setup: beta must have exactly one UNDISPATCHED queued item"
        )
        await reg.attach("alpha")

        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)

            # Non-vacuity guard (see docstring): a harmless frame on alpha,
            # settled BEFORE the switch, so the generic "seed on first frame
            # the pump ever processes" check has already fired here (on
            # alpha) rather than on the session_attached frame below — the
            # ONLY remaining seed path for beta is the eager reseed inside
            # the switch handler itself.
            transport.push_display(OutboxMessage(kind="user", text="alpha noop"))
            await _settle(pilot)

            # NOTE: flips the registry's connection pointer directly (#3793
            # stage 1: ``AttachedConnection.switch``) rather than calling
            # ``reg.attach("beta")`` again — the auto-driver was deliberately
            # stopped above (so THIS test controls dispatch); a real
            # ``attach()`` call re-boots a fresh ``session.run()`` background
            # task (its own ``_tasks[key].done()`` check sees the cancelled
            # task and reboots), which would race the manually-held-busy
            # turn for the SAME inbox and silently dispatch the "queued"
            # fixture item out from under this test. Flipping the pointer
            # directly is the read-side-only equivalent this test needs —
            # the app's ``_snapshot()`` only ever reads
            # ``registry.attached_session()``, never re-derives from a live
            # ``attach()`` call itself.
            reg._connection.switch(("beta", _DEFAULT_SID))
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            sent_queue = app.query_one(SentQueue)
            assert sent_queue.has_items() is True, (
                "beta's OWN already-queued item must render once this client "
                "switches to beta — the switch-barrier reseed must fire"
            )
            assert any(
                "beta queued item" in t for t in sent_queue.rendered_texts()
            ), sent_queue.rendered_texts()

        _llm_stub.release.set()
        await turn_task

        # witness ②: the real driver dispatched beta's own turn.
        await beta._audit_events.drain()
        assert any(e.type == "turn_started" for e in events)
    finally:
        await reg.shutdown()


# ── 4. unvisited session ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unvisited_session_shows_its_history_not_blank(tmp_path, monkeypatch) -> None:
    """Tier 2: switching to a session never seen in THIS client run (the
    client mounted attached to alpha; beta was never attached-to by this
    client before) shows BETA's persisted history, not a blank pane — the
    cache-miss path is just "hydrate", the same as any other switch, since
    there is no cache to miss in the first place (v1 has none)."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        # beta accumulates history entirely BEFORE this client ever attaches
        # to it — via a SEPARATE attach the test uses only to seed history,
        # never surfaced to this app/client.
        beta = await reg.attach("beta")
        beta._append_history(ChatMessage(role="user", content="beta turn 1"))
        beta._append_history(ChatMessage(role="assistant", content="beta reply 1"))
        await reg.attach("alpha")

        transport = QueueTransport()
        app = TextualChatApp(
            transport=transport, read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            assert _rows(app) == [], "alpha (this client's mount session) has no history"

            await reg.attach("beta")
            transport.push_event(
                "session_attached", {"agent": "beta", "session_id": _DEFAULT_SID}
            )
            await _settle(pilot)

            assert _rows(app) == [
                ("system", "⤺ resumed previous conversation"),
                ("user", "beta turn 1"),
                ("agent", "beta reply 1"),
            ], (
                "a never-before-attached-in-this-client-run session must "
                f"hydrate from its history, not show blank: {_rows(app)!r}"
            )
    finally:
        await reg.shutdown()
