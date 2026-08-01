"""Tier 2: /reload slash command — handler behaviour (no-reloader + happy path).

``/reload`` schedules a runtime config hot-reload via ``session._hot_reloader``.
Two paths are untested elsewhere:

* absent reloader (``_hot_reloader is None``) → graceful error, no crash;
* present reloader → ``request_reload(source="operator")`` + success reply.

The registry-membership smoke is already in ``test_2073_s1_hot_reloader.py``
and is not duplicated here.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.reload import reload_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session.outbox_calls)


class _FakeSession:
    def __init__(self, *, hot_reloader=None) -> None:
        self._hot_reloader = hot_reloader
        self.outbox_calls: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_calls.append(msg)


class _StubReloader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_reload(self, *, source: str) -> None:
        self.calls.append({"source": source})


@pytest.mark.asyncio
async def test_reload_no_reloader_is_graceful_error() -> None:
    """Tier 2: /reload with no _hot_reloader wired replies an error, not a crash."""
    session = _FakeSession(hot_reloader=None)
    await reload_cmd(_ctx(session), "")
    errors = [m for m in session.outbox_calls if m.kind == "error"]
    assert errors, f"expected error reply; got {[m.kind for m in session.outbox_calls]}"


@pytest.mark.asyncio
async def test_reload_with_reloader_calls_request_reload_operator() -> None:
    """Tier 2: /reload with a live reloader calls request_reload(source='operator')."""
    reloader = _StubReloader()
    session = _FakeSession(hot_reloader=reloader)
    await reload_cmd(_ctx(session), "")
    assert reloader.calls == [{"source": "operator"}]


@pytest.mark.asyncio
async def test_reload_with_reloader_sends_success_not_error() -> None:
    """Tier 2: /reload happy path sends a system reply, never an error."""
    reloader = _StubReloader()
    session = _FakeSession(hot_reloader=reloader)
    await reload_cmd(_ctx(session), "")
    kinds = [m.kind for m in session.outbox_calls]
    assert "system" in kinds, f"expected system reply; got {kinds}"
    assert "error" not in kinds
