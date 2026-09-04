"""Tier 2: #5729 — the agent pane's per-session rows carry turn_active/
iv_waiting as 2 INDEPENDENT glyph slots, sourced from a REAL
``AgentRegistry.all_sessions_status()`` riding the real ``_snapshot()``
producer — never collapsed into one status indicator (architect ruling:
"turn dispatched AND waiting on an answer" is the one combination that
matters most to an operator, and a single glyph could not carry it).

Real ``AgentRegistry``/``Session`` throughout, mirroring
``test_3338_tui_status_chrome_liveness.py``'s own ``_real_snapshot``
producer (a local copy, not a cross-file import — this session's own
established convention, #5588)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.chrome import pane_commands, pane_payload
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session

AGENT = "chrome-5729-agent"


async def _real_snapshot(tmp_path: Path) -> "tuple[dict, Session, AgentRegistry]":
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            registry=holder.get("reg"),
        )

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = registry
    AgentProfile.new(AGENT, role="").save(tmp_path / ".reyn" / "agents" / AGENT)
    session = await registry.attach(AGENT)
    snap = _snapshot(registry)
    assert snap is not None, "the real producer returned no snapshot"
    return snap, session, registry


@pytest.mark.asyncio
async def test_all_sessions_status_rides_the_real_snapshot(tmp_path) -> None:
    """Tier 2: the real ``_snapshot()`` producer carries ``all_sessions_status``
    — an idle attached session shows both bools False."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    rows = snap["all_sessions_status"]
    assert rows == [{"agent": AGENT, "sid": "main", "turn_active": False, "iv_waiting": False}]


@pytest.mark.asyncio
async def test_agent_pane_shows_no_glyphs_while_idle(tmp_path) -> None:
    """Tier 2: deny side — an idle session's row carries neither glyph."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if "main" in r)
    assert "●" not in session_row
    assert "?" not in session_row


@pytest.mark.asyncio
async def test_agent_pane_shows_both_glyphs_together_never_collapsed(tmp_path) -> None:
    """Tier 2: the architect's central ruling, at the rendered-row level —
    when a session is BOTH turn_active and iv_waiting, the row carries BOTH
    glyphs, not one collapsed indicator. Driven by directly recomputing the
    pane payload against a hand-built snapshot dict carrying real
    ``all_sessions_status``-shaped rows (the pure-function boundary
    ``_agent_pane_entries`` actually renders from) — the session-driving
    half of this claim (can both bools genuinely be True at once) is
    covered end-to-end in
    tests/runtime/test_5729_status_registry_wiring.py; this test is the
    presentation half."""
    snap, _session, registry = await _real_snapshot(tmp_path)
    tree = snap["session_tree"]
    sid = tree[0]["sessions"][0]["sid"]
    snap = {**snap, "all_sessions_status": [
        {"agent": AGENT, "sid": sid, "turn_active": True, "iv_waiting": True},
    ]}
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if sid in r)
    assert "●" in session_row, f"turn_active glyph missing: {session_row!r}"
    assert "?" in session_row, f"iv_waiting glyph missing: {session_row!r}"

    cmds = pane_commands("agent", snap)
    assert len(cmds) == len(rows), "agent rows and their commands drifted apart"


@pytest.mark.asyncio
async def test_agent_pane_status_is_process_scoped_never_fabricated_for_a_sibling(
    tmp_path,
) -> None:
    """Tier 2: deny side — a session absent from ``all_sessions_status``
    (e.g. a sibling process's session, #5729's own process-scope limit)
    renders blank glyphs, never a fabricated "not running" mark. Simulated
    here by simply omitting the row (this process genuinely cannot see a
    sibling process's session — there is no live one to construct)."""
    snap, _session, _registry = await _real_snapshot(tmp_path)
    snap = {**snap, "all_sessions_status": []}
    rows = pane_payload("agent", snapshot=snap)
    session_row = next(r for r in rows if "main" in r)
    assert "●" not in session_row
    assert "?" not in session_row


# ── the real end-to-end reactivity witness (#5734 review finding) ──────────


@pytest.mark.asyncio
async def test_agent_pane_reacts_to_an_unattached_sessions_status_with_no_frame(
    tmp_path,
) -> None:
    """Tier 2: lead-coder's #5734 review, 2 rounds of BLOCKING findings.

    Round 1 — ``add_status_listener`` had ZERO production call sites; the
    pull side (all_sessions_status -> snapshot -> chrome) is only re-read
    when a FRAME lands (``_refresh_live_chrome``), which never happens for
    a session this TUI has not attached.

    Round 2 — even wired, the push covered only 1 of the 6 real
    ``intervention_bus.request()`` callers: only ``ask_user.py`` emits
    ``user_intervention_requested`` directly (verified,
    ``interfaces/repl/renderer.py``'s own comment); permission confirm —
    the most common real-world intervention — is one of the other 5 and
    would never push at all. This test drives a ``kind="permission.
    generic"`` intervention (deliberately NOT ``ask_user``) through the
    REAL ``dispatch()`` (never a synthetic audit-event emit) and asserts
    the agent pane reacts with a transport of ZERO frames AND zero
    ``user_intervention_requested`` events — proving the NEW
    ``intervention_announced`` audit event (emitted from
    ``InterventionHandler.announce``, the choke point every caller
    shares), not the ask_user-only event, is what actually carries it.

    Real ``AgentRegistry``/``Session``/``RegistryReadModel``/
    ``TextualChatApp`` throughout — ``app.on_mount``'s real
    ``read_model.add_status_listener(self._on_session_status_delta)`` call
    is the thing under test, not a stand-in for it."""
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp
    from reyn.interfaces.repl.read_model import RegistryReadModel

    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = registry
    AgentProfile.new(AGENT, role="").save(tmp_path / ".reyn" / "agents" / AGENT)
    await registry.attach(AGENT)  # session A — this is what the TUI attaches to
    sid_b = registry.spawn_session(AGENT, presentation_consumer=None, intervention_bridge=None)
    session_b = registry.get_session(AGENT, sid_b)
    assert session_b is not None
    # A real Session's InterventionRegistry short-circuits dispatch()
    # (enforce_listener_presence=True, #254 Phase 1) with no listener —
    # register one so the drive below actually enqueues.
    session_b.register_intervention_listener("test")

    read_model = RegistryReadModel(registry, agent_name=AGENT)
    transport = _EventOnlyTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._open_drawer("agent")
        await pilot.pause()

        def _rows() -> list[str]:
            opts = app.query_one("#agent", OptionList)
            return [str(opts.get_option_at_index(i).prompt) for i in range(opts.option_count)]

        before = next(r for r in _rows() if sid_b in r)
        assert "?" not in before, f"session B row already shows iv_waiting before it was queued: {before!r}"

        # Constructed INSIDE the coroutine (a UserIntervention's future
        # binds to whatever loop is current at construction). ``kind=
        # "permission.generic"`` deliberately, NOT "ask_user" — this is
        # lead-coder's #5734 follow-up finding: 5 of the 6
        # intervention_bus.request() callers (permission confirm being the
        # most common in real use) never emit user_intervention_requested
        # at all; the signal common to all 6 is the NEW
        # intervention_announced audit event (InterventionHandler.
        # announce's own emit). Driving this via the REAL dispatch() (not
        # a synthetic audit-event emit) is what actually exercises that
        # shared path.
        iv = UserIntervention(kind="permission.generic", prompt="Allow tool 'shell'?")
        events = []
        session_b.subscribe_audit_events(events.append)
        iv_task = asyncio.ensure_future(session_b.interventions.dispatch(iv))
        try:
            await asyncio.sleep(0)  # let dispatch() enqueue + announce() fire
            await pilot.pause()
            assert not any(e.type == "user_intervention_requested" for e in events), (
                "this path must reach the pane with NO user_intervention_requested "
                "audit event — intervention_announced is the mechanism under test, not "
                "a fallback to the ask_user-only event"
            )

            after = next(r for r in _rows() if sid_b in r)
            assert "?" in after, (
                f"agent pane did not react to session B's iv_waiting with no "
                f"frame sent: {after!r}"
            )

            # ★ the pair, not just the True half (lead-coder's own #5734
            # follow-up warning: a fabricated "waiting" that never clears
            # is worse than one that never lit). Resolving via
            # ``Session.answer_intervention_by_id`` — the REAL production
            # path — not ``InterventionRegistry.deliver_answer`` directly,
            # which bypasses the ``user_answered_intervention`` audit emit
            # entirely and would prove nothing about the OUT side.
            resolved = await session_b.answer_intervention_by_id(iv.id, "ok")
            assert resolved is True
            await iv_task
            # Unbounded — CI's own timeout is the kill switch (CLAUDE.md:
            # no attempts=N/range(N) wrapping a wait). A real failure here
            # (never clearing) hangs until CI's timeout, not a guessed
            # attempt count that a slower CI run could outrun.
            cleared = next(r for r in _rows() if sid_b in r)
            while "?" in cleared:
                await pilot.pause()
                cleared = next(r for r in _rows() if sid_b in r)
        except BaseException:
            if not iv_task.done():
                await session_b.interventions.deliver_answer(iv, "ok")
                await iv_task
            raise


@pytest.mark.asyncio
async def test_on_unmount_removes_the_status_listener(tmp_path) -> None:
    """Tier 2: teardown half of the same finding — ``on_unmount`` calls
    ``remove_status_listener`` so repeated attach/detach (a real operator
    workflow) does not accumulate dead listeners on the registry. Reads
    the public ``status_listener_count()`` witness, never the private
    ``_status_listeners`` list directly."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp
    from reyn.interfaces.repl.read_model import RegistryReadModel

    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = registry
    AgentProfile.new(AGENT, role="").save(tmp_path / ".reyn" / "agents" / AGENT)
    await registry.attach(AGENT)

    read_model = RegistryReadModel(registry, agent_name=AGENT)
    transport = _EventOnlyTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)

    assert registry.status_listener_count() == 0
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert registry.status_listener_count() == 1, "on_mount did not register a listener"

    assert registry.status_listener_count() == 0, (
        "on_unmount did not remove the listener — it leaked past app teardown"
    )


# ── local minimal ClientTransport (mirrors test_3338's own _EventOnlyTransport,
#    a local copy per this session's established convention, not a cross-file
#    import) ───────────────────────────────────────────────────────────────


class _EventOnlyTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` — no display/event queue
    actually used by these tests (they drive state directly on the real
    Session objects, never through the transport), but ``TextualChatApp``
    needs a concrete, non-abstract instance to construct and run. Same
    override set as test_3338_tui_status_chrome_liveness.py's own
    ``_EventOnlyTransport`` (a local copy, not a cross-file import — this
    session's established convention, #5588)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self):
        while True:
            yield await self._queue.get()  # pragma: no cover - never actually sent

    async def submit_user_text(self, text: str) -> str:  # pragma: no cover
        return ""

    async def run_slash_command(self, name: str, args: str) -> bool:  # pragma: no cover
        return True

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover - trivial
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass
