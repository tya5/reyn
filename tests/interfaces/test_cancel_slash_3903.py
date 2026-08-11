"""Tier 2: #3903 — /cancel slash command, and Session.cancel_inflight()'s
accurate (not unconditional) result summary.

Pre-#3903, ``Session.cancel_inflight()`` returned ``"✗ cancelled turn"``
unconditionally — even when nothing was in flight — the same shape #4166
found live in ``cancel_task``'s own reply (a request accepted and reported
as successful regardless of the actual outcome). ``ClientTransport.
cancel_inflight()``'s return type changed from ``None`` to ``str`` so
``/cancel`` (the missing user-facing口 owner ruled must exist) can surface
the real outcome instead of a blanket "done".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.slash.cancel import cancel_cmd
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.slash import RecordingTransport, slash_ctx

AGENT = "cancel-slash-agent"


def _make_session(tmp_path: Path) -> Session:
    from reyn.core.events.state_log import StateLog

    state_log = StateLog(tmp_path / "state.wal")
    return make_session(
        agent_name=AGENT, state_log=state_log, snapshot_path=tmp_path / "snapshot.json",
    )


@pytest.mark.asyncio
async def test_cancel_inflight_reports_nothing_running_when_nothing_is(tmp_path):
    """Tier 2: a fresh session with no in-flight turn and no cancel-forward
    targets returns an outcome distinguishable from a real cancel — not the
    same "✗ cancelled turn" string a genuine cancel returns."""
    session = _make_session(tmp_path)
    result = await session.cancel_inflight()
    assert "cancel" not in result.lower(), (
        f"a no-op cancel_inflight() must not claim it cancelled anything: {result!r}"
    )
    assert result == "nothing was running"


@pytest.mark.asyncio
async def test_cancel_inflight_reports_a_forwarded_cancel_as_something_happening(tmp_path):
    """Tier 2: accept-side sibling — when a cancel-forward target IS
    registered (the #2588 pipeline-attach shape), cancel_inflight() must NOT
    report "nothing was running" even though THIS session's own
    _turn_owner_task is None; something was still cancelled."""
    session = _make_session(tmp_path)
    forwarded: list[bool] = []
    unregister = session.register_cancel_forward(lambda: forwarded.append(True))
    try:
        result = await session.cancel_inflight()
    finally:
        unregister()
    assert forwarded == [True]
    assert result != "nothing was running"


@pytest.mark.asyncio
async def test_cancel_cmd_reply_echoes_the_transport_summary(tmp_path):
    """Tier 2: /cancel's reply is whatever ctx.transport.cancel_inflight()
    reports — not a hardcoded "done" independent of the real outcome (the
    #4166 shape this command exists to avoid repeating)."""
    session = _make_session(tmp_path)
    ctx = slash_ctx(session)
    await cancel_cmd(ctx, "")
    transport = ctx.transport
    assert isinstance(transport, RecordingTransport)
    texts = [m.text for m in transport.displayed]
    assert any("nothing was running" in t for t in texts), texts


@pytest.mark.asyncio
async def test_cancel_cmd_reports_a_real_cancel_distinctly(tmp_path):
    """Tier 2: falsify pair — with a cancel-forward target registered (so
    cancel_inflight() reports something DID happen), /cancel's reply must
    say something different from the nothing-running case above."""
    session = _make_session(tmp_path)
    unregister = session.register_cancel_forward(lambda: None)
    try:
        ctx = slash_ctx(session)
        await cancel_cmd(ctx, "")
    finally:
        unregister()
    texts = [m.text for m in ctx.transport.displayed]
    assert not any("nothing was running" in t for t in texts), texts
    assert any("cancel" in t.lower() for t in texts), texts
