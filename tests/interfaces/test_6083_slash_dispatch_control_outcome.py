"""Tier 2: #6083 stage 1 — a session-locus slash command's own "could not
run" line reflects the REAL typed ``ControlOutcome`` (delivered / refused /
not_delivered), never a hardcoded "this client has no session" claim.

owner-hit (reyn-self, real machine): a `/compact` that had ALREADY
SUCCEEDED server-side (2,011 turns folded, window 48%) was reported as
"could not run: this client has no session to run it on. — the server did
not respond within 10s [ReadTimeout]" — the suffix (from
``with_control_failure``) was already correct, but the hardcoded PREFIX was
not: a reader parses "<claim>. — <detail>" as the claim plus supporting
detail, not the detail correcting a false claim. #5907 ②'s own
``ControlOutcome`` already distinguishes exactly this (delivered / refused /
not_delivered); this fix stops a session-locus command's own text from
guessing "no session" ahead of it.

⚠️ This is stage 1 only — it stops the false CLAIM, it does not make a
successful `/compact` stop taking longer than the control read timeout (see
#6083's own PR body for stage 2).

Real ``AgUiTransport`` throughout (the same "plain async `_send` callable"
precedent ``test_3300_p3_cancel_by_id.py`` already established for this
exact class) — no mock of the transport or of ``ControlOutcome``.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from reyn.interfaces.slash import REGISTRY, SlashCommand
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.control_outcome import ControlOutcome
from reyn.runtime.outbox import OutboxMessage


async def _empty_lines() -> "AsyncIterator[str]":
    return
    yield  # pragma: no cover


async def _noop_handler(ctx, args: str) -> None:  # pragma: no cover - never called client-side
    pass


@pytest.fixture
def _throwaway_session_command(monkeypatch):
    """A registered, throwaway ``locus="session"`` command — its own
    ``handler`` never runs client-side (session-locus dispatches via
    ``transport.run_slash_command``, never ``cmd.handler`` directly; see
    ``dispatch.py``'s own ``_run()``), so a no-op stand-in is enough."""
    monkeypatch.setitem(
        REGISTRY._commands, "__f6083test__",
        SlashCommand(
            name="__f6083test__", summary="test", handler=_noop_handler, locus="session",
        ),
    )
    return "__f6083test__"


@pytest.mark.asyncio
async def test_a_control_read_timeout_does_not_claim_no_session(
    _throwaway_session_command: str,
) -> None:
    """Tier 2: non-vacuity — ``not_delivered`` (the owner's own #6083 case,
    a control read timeout) must not say "no session"."""
    async def _send(payload: dict) -> "ControlOutcome":
        return ControlOutcome.not_delivered("ReadTimeout", 10.0)

    transport = AgUiTransport(_empty_lines(), _send)
    captured: "list[OutboxMessage]" = []
    transport.put_display = captured.append  # type: ignore[method-assign]

    consumed = await maybe_dispatch_slash(transport, f"/{_throwaway_session_command}", echo=False)
    assert consumed is True

    error_lines = [m.text for m in captured if m.kind == "error"]
    assert error_lines, "expected an error line for the failed session-locus command"
    text = error_lines[-1]
    assert "no session" not in text, (
        f"a control-read-timeout was reported as 'no session' -- the exact "
        f"#6083 owner-hit symptom, text was: {text!r}"
    )
    assert "did not respond" in text and "ReadTimeout" in text, (
        f"expected the REAL typed outcome (not_delivered) in the text, got: {text!r}"
    )


@pytest.mark.asyncio
async def test_a_server_refusal_says_refused_not_no_session(
    _throwaway_session_command: str,
) -> None:
    """Tier 2: the sibling typed outcome (``refused``) must also read as
    what actually happened, not the same hardcoded guess."""
    async def _send(payload: dict) -> "ControlOutcome":
        return ControlOutcome.refused(403, "forbidden")

    transport = AgUiTransport(_empty_lines(), _send)
    captured: "list[OutboxMessage]" = []
    transport.put_display = captured.append  # type: ignore[method-assign]

    consumed = await maybe_dispatch_slash(transport, f"/{_throwaway_session_command}", echo=False)
    assert consumed is True

    error_lines = [m.text for m in captured if m.kind == "error"]
    assert error_lines
    text = error_lines[-1]
    assert "no session" not in text
    assert "refused" in text


@pytest.mark.asyncio
async def test_client_locus_never_touches_the_wire(monkeypatch) -> None:
    """Tier 2: deny side, scope check — a CLIENT/CONNECTION-locus command
    runs entirely in-process (``execute_slash_command``, no control POST —
    the ``locus`` comment in ``dispatch.py``'s own ``_run()`` states this
    directly) and its own "no session" wording is UNCHANGED by #6083 (that
    branch's own ``ran`` never comes from a ``ControlOutcome`` at all, so
    there is nothing to un-crush there). Proven by making the transport's
    own send raise if ever called -- a client-locus dispatch that reached
    the wire would fail this test, not merely mis-word its error."""
    async def _handler(ctx, args: str) -> None:
        pass

    monkeypatch.setitem(
        REGISTRY._commands, "__f6083client__",
        SlashCommand(
            name="__f6083client__", summary="test", handler=_handler, locus="client",
        ),
    )

    async def _send(payload: dict) -> "ControlOutcome":
        raise AssertionError("client-locus must never touch the wire")

    transport = AgUiTransport(_empty_lines(), _send)

    consumed = await maybe_dispatch_slash(transport, "/__f6083client__", echo=False)
    assert consumed is True
