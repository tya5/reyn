"""Tier 2: #4497 Phase 1 — `/resident` slash command.

Drives the real handler through a real ``SlashContext`` + ``Session``
(``tests._support.slash.slash_ctx`` / ``tests._support.agent_session.make_session``)
— no mocks. ``resident.py`` itself is thin (delegates all attribute
reading to ``resident_stats.py``, tested directly in
``test_4497_resident_stats.py``); this file's job is the wiring: the
command is reachable, replies through the transport, and its rendered
text names the containers it claims to measure.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.resident import resident_cmd
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx


@pytest.mark.asyncio
async def test_resident_replies_through_the_transport():
    """Tier 2: the command's output reaches the client via reply(), the
    same seam every other slash command uses."""
    session = make_session(agent_name="resident-slash-test")
    ctx = slash_ctx(session)

    await resident_cmd(ctx, "")

    assert ctx.transport.kinds() == ["system"]


@pytest.mark.asyncio
async def test_resident_output_names_the_enumerated_containers():
    """Tier 2: the reply text is not just non-empty — it names real
    container attribute names from #4497's own enumeration, so a reader
    can actually find "what's dominant" rather than getting an opaque
    blob."""
    session = make_session(agent_name="resident-slash-test")
    ctx = slash_ctx(session)

    await resident_cmd(ctx, "")

    text = ctx.transport.system_text()
    assert "_pending_user_attachments" in text
    assert "_allowed_mcp" in text
    assert "_session_bridges" in text
    assert "_REWIND_INDEXES" in text


@pytest.mark.asyncio
async def test_resident_reflects_a_real_session_write():
    """Tier 2: end-to-end — a real write to the session's own container
    shows up in the command's rendered count, not just in the
    lower-level module's own unit tests."""
    session = make_session(agent_name="resident-slash-test")
    session._pending_user_attachments.extend([{"data": "x"}, {"data": "y"}, {"data": "z"}])
    ctx = slash_ctx(session)

    await resident_cmd(ctx, "")

    text = ctx.transport.system_text()
    row = next(line for line in text.splitlines() if "_pending_user_attachments" in line)
    assert "3" in row


@pytest.mark.asyncio
async def test_resident_is_registered_in_the_slash_registry():
    """Tier 2: (reachability) the command is actually wired into the real
    registry an operator's typed `/resident` dispatches through, not just
    importable in isolation — the same "declared/implemented/tested/
    invoked-by-nobody" shape this session has flagged repeatedly
    elsewhere tonight."""
    from reyn.interfaces.slash import REGISTRY

    cmd = REGISTRY.get("resident")
    assert cmd is not None
    assert cmd.handler is resident_cmd
