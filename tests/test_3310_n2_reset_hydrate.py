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
3. Per-state reset: one independent test per state (``_running_tools``,
   ``_pending_ivs``/panel tabs, the sent-queue view/widget/`_queue_item_meta`,
   ``_streaming_replies``), each with its OWN discriminating witness (never
   folded into a shared test — the #3302/#3308 sibling-guard-site lesson).
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
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

# ── real seam: a queue-backed ClientTransport a test drives frame-by-frame ──


class QueueTransport(ClientTransport):
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
        return False

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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


@pytest.mark.asyncio
async def test_reset_pending_interventions_closes_all_tabs(tmp_path, monkeypatch) -> None:
    """Tier 2: ``_pending_ivs`` / panel-tabs witness. A pending intervention on
    alpha opens the panel with one tab; switching away must close the whole
    panel (``InterventionPanel.collapse_all`` — the whole panel, panel.display
    False, zero mounted ``TabPane``s), never leaving alpha's stale tab
    lingering behind the new session's own (server-re-announced) ones."""
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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


@pytest.mark.asyncio
async def test_reset_sent_queue_view_and_widget_and_item_meta(tmp_path, monkeypatch) -> None:
    """Tier 2: ``_queue_view`` / ``_queue_seeded`` / ``_queue_item_meta`` / the
    :class:`SentQueue` widget rows — witnessed together because they are ONE
    observable surface (the widget's public ``has_items()``/
    ``rendered_texts()``), but the underlying reset is driven by clearing ALL
    FOUR: a fresh ``RemoteQueueView()``, ``_queue_seeded=False`` (so the
    existing seed-on-first-frame path re-seeds the NEW session), the sent-queue
    WIDGET's rows, and the meta side-table. A queued item materializes via a
    ``user_submitted`` chat-event (the real production path,
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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


def _install_hanging_run_turn(session: Session):
    """Same no-mock seam ``tests/test_3300_p2a_queue_state_publish.py`` uses
    (itself mirroring ``tests/test_2242_hard_cancel.py``): a real, plain
    async function method-assigned onto the instance standing in for a
    genuinely in-flight LLM call, so a turn can be held BUSY (never touching
    a real LLM) while a SECOND submission queues behind it."""
    call_started = asyncio.Event()
    release = asyncio.Event()

    async def _hanging_run_turn(user_text: str, chain_id: str) -> None:
        call_started.set()
        await release.wait()

    session._loop_driver.run_turn = _hanging_run_turn  # type: ignore[method-assign]
    return call_started, release


async def _stop_auto_driver(reg: AgentRegistry, name: str) -> None:
    """Cancel the background ``session.run()`` driver ``AgentRegistry.attach``
    booted for ``name`` — the SAME helper
    ``tests/test_3300_p2a_queue_state_publish.py`` uses, so a test can drive
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
async def test_reset_queue_view_reseeds_from_new_sessions_own_queue(
    tmp_path, monkeypatch,
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
    path specifically."""
    monkeypatch.chdir(tmp_path)
    reg = _registry_with_wal(tmp_path)
    try:
        await reg.attach("alpha")
        beta = await reg.attach("beta")
        await _stop_auto_driver(reg, "beta")

        call_started, release = _install_hanging_run_turn(beta)
        await beta.submit_user_text("beta busy trigger")
        turn_task = asyncio.create_task(beta.run_one_iteration())
        await asyncio.wait_for(call_started.wait(), timeout=5)

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

            # NOTE: flips ``registry._attached`` directly rather than calling
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
            reg._attached = ("beta", _DEFAULT_SID)
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

        release.set()
        await asyncio.wait_for(turn_task, timeout=5)
    finally:
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)


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
        await asyncio.wait_for(reg.shutdown(), timeout=5.0)
