"""Tier 2: #5139 C — the wire round-trip for a client-driven older-backlog
pull: real ``session_backlog_page`` (server-side pagination) → real
``encode_messages_snapshot`` → real ``AgUiTransport.request_older_backlog``
(client-side decode). Complements ``test_5139c_page_restored_history.py``
(the pure pagination logic, no wire) and
``test_5139c_remote_reached_top_pull.py`` (the app-side ``ReachedTop``
wiring, a fixture transport) — this file is the one that proves the ACTUAL
encode/decode pair a real server response uses round-trips correctly,
including a genuine 2-page split.

Real ``AgentRegistry``/``Session``/``AgUiTransport``/``session_backlog_page``/
``encode_messages_snapshot`` throughout — no mocks. The HTTP/ASGI POST layer
itself is not driven here (``request_older_backlog``'s ``_send`` stub returns
what a real endpoint handler would build via those same 2 real functions,
matching this module's own established boundary — other typed-request tests
in this suite stop at the same seam, e.g. ``test_3310``'s own module
docstring)."""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.endpoint import session_backlog_page
from reyn.interfaces.transport.agui.protocol import encode_messages_snapshot
from reyn.interfaces.transport.frames import HYDRATE_PAGE_FRAMES, BacklogBatch
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from tests._support.agent_session import make_session


def _registry(tmp_path):
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


@pytest.mark.asyncio
async def test_request_older_backlog_decodes_a_real_second_page(tmp_path) -> None:
    """Tier 2: a history long enough to force a genuine 3-page split: the FIRST
    page (what a real connect/switch would send) reports ``has_more=True``
    with a real cursor; POSTing that cursor through a real
    :meth:`AgUiTransport.request_older_backlog` decodes the SECOND page —
    which must ITSELF still report ``has_more=True`` with a real,
    DIFFERENT cursor (a 3rd page still remains) — deliberately not the
    all-defaults-are-already-False shape a final page would have, so a
    stripped ``has_more``/``next_cursor`` decode (defaulting to
    ``False``/``None``) is distinguishable from a correctly-decoded
    ``True``/real-id (verified locally: reverting ``decode_event``'s
    ``messages`` branch to the pre-#5139-C ``MessagesSnapshot(frames=frames)``
    does NOT flip this test red for a final-page fixture, but DOES for
    this one)."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        session = reg.get_session("alpha", _DEFAULT_SID)
        assert session is not None
        # 1 turn (user + assistant) projects to 2 frames — enough turns to
        # span 3 pages (page 2 must itself still have a remainder).
        n_turns = HYDRATE_PAGE_FRAMES + 20
        for i in range(n_turns):
            cid = f"turn-{i}"
            session.history.append(
                ChatMessage(role="user", content=f"msg {i}", meta={"chain_id": cid})
            )
            session.history.append(
                ChatMessage(role="assistant", content=f"reply {i}", meta={"chain_id": cid})
            )

        page1_frames, has_more1, cursor1 = session_backlog_page(reg, "alpha", _DEFAULT_SID)
        assert has_more1 is True, "test setup must force a real page split"
        assert cursor1 is not None

        async def _send(payload: dict) -> dict:
            assert payload["type"] == "load_older_backlog_request"
            assert payload["before_root_id"] == cursor1
            page2_frames, has_more2, cursor2 = session_backlog_page(
                reg, "alpha", _DEFAULT_SID, before_root_id=payload["before_root_id"],
            )
            event = encode_messages_snapshot(page2_frames, has_more=has_more2, next_cursor=cursor2)
            return {"status": "ok", **event.data}

        async def _no_sse():
            return
            yield  # pragma: no cover - makes this an async generator

        transport = AgUiTransport(_no_sse(), _send, agent_name="alpha")
        # Seed the destination this transport's own BacklogBatch construction
        # stamps (normally set by the ``session_attached`` announce that
        # precedes a real MESSAGES_SNAPSHOT on the wire) so the resulting
        # batch is not discarded as stale by whatever consumes it.
        transport._backlog_sid = _DEFAULT_SID  # noqa: SLF001 - test seeds transport-internal destination state

        await transport.request_older_backlog(cursor1)

        # Drain exactly what request_older_backlog queued (no live SSE
        # source is running here, so frames() would otherwise hang forever
        # waiting on _pump_sse — reading the queue directly instead).
        item = transport._display_queue.get_nowait()  # noqa: SLF001 - direct queue read, no live SSE source in this test
        assert isinstance(item, BacklogBatch)
        assert item.is_older_page is True
        assert item.has_more is True, (
            "test setup must leave a 3rd page remaining, so this assertion "
            "distinguishes a correctly-decoded True from a stripped decode's "
            "always-False default"
        )
        assert item.next_cursor is not None and item.next_cursor != cursor1
        assert len(item.frames) > 0
        # Content parity: the SAME messages session_backlog_page's own page 2
        # produced, decoded back out the other side of the wire.
        page2_frames, _, _ = session_backlog_page(reg, "alpha", _DEFAULT_SID, before_root_id=cursor1)
        expected_texts = [f.message.text for f in page2_frames]
        got_texts = [f.message.text for f in item.frames]
        assert got_texts == expected_texts
    finally:
        await reg.shutdown()
