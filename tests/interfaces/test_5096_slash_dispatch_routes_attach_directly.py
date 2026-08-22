"""Tier 2: #5096 ②, architect ruling (issuecomment-5379623427/5379638878/
5379657592) — ``maybe_dispatch_slash`` routes a ``connection``-locus
command (``/attach``) straight to ``ClientTransport.request_attach``,
never through the generic ``run_slash_command`` forward.

The real owner-reported defect (#5094, lead-coder's diagnosis,
issuecomment-5379598384): ``/attach coder-smith`` used to ALWAYS forward
through ``transport.run_slash_command("attach", "coder-smith")`` —
correct for a "session"-locus command, but for a REMOTE (``--connect``)
client this lands on the SERVER's generic slash dispatch, which builds a
``SlashContext`` over ``SessionBoundTransport`` (send-side only,
structurally unable to answer "attach a different agent") instead of
ever reaching ``AgUiTransport``'s own correctly-implemented
``request_attach`` (a wire call to the dedicated ``attach_request``
typed-op handler). This test drives the CLIENT-side dispatch layer
directly and asserts the typed op is called, not the generic forward.

Real ``ClientTransportStub`` throughout (the tests/-only stub base,
#5076) — no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.client_transport import ClientTransportStub


class _RecordingTransport(ClientTransportStub):
    """Records which of ``request_attach``/``run_slash_command`` the
    dispatch layer actually called, and with what -- the discriminator
    this test's own assertion needs."""

    def __init__(self) -> None:
        self.displayed: "list[object]" = []
        self.request_attach_calls: "list[str]" = []
        self.run_slash_command_calls: "list[tuple[str, str]]" = []

    def start(self) -> None: ...
    def close(self) -> None: ...

    async def frames(self):
        return
        yield  # pragma: no cover — makes this an async generator

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(self, text, *, intervention_id=None):
        return False

    async def answer_intervention_choice(self, choice_id, *, intervention_id=None):
        return False

    def has_session(self) -> bool:
        return False

    def pending_intervention_head(self):
        return None

    def put_display(self, msg) -> None:
        self.displayed.append(msg)

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None: ...

    async def request_attach(self, agent_name: str) -> bool:
        self.request_attach_calls.append(agent_name)
        return True

    async def run_slash_command(self, name: str, args: str) -> bool:
        self.run_slash_command_calls.append((name, args))
        return False


@pytest.mark.asyncio
async def test_attach_calls_request_attach_directly_never_run_slash_command() -> None:
    """Tier 2: /attach's connection-locus dispatch calls
    ClientTransport.request_attach directly, never the generic
    run_slash_command forward — see this module's own docstring."""
    transport = _RecordingTransport()

    consumed = await maybe_dispatch_slash(transport, "/attach coder-smith", echo=False)

    assert consumed is True
    assert transport.request_attach_calls == ["coder-smith"], (
        "the /attach command must call ClientTransport.request_attach "
        "directly (the connection-locus dispatch path) -- got "
        f"{transport.request_attach_calls!r}"
    )
    assert transport.run_slash_command_calls == [], (
        "attach must NOT go through the generic run_slash_command forward "
        "-- that is exactly the path that lands on SessionBoundTransport "
        "server-side and silently fails (#5094's own root cause); got "
        f"{transport.run_slash_command_calls!r}"
    )


@pytest.mark.asyncio
async def test_a_session_locus_command_still_uses_run_slash_command() -> None:
    """Tier 2: positive control / non-regression — a "session"-locus
    command (e.g. /cost) must still forward through run_slash_command,
    proving the branch genuinely discriminates by locus rather than
    always taking the connection-locus path."""
    transport = _RecordingTransport()

    consumed = await maybe_dispatch_slash(transport, "/cost", echo=False)

    assert consumed is True
    assert transport.run_slash_command_calls == [("cost", "")]
    assert transport.request_attach_calls == []
