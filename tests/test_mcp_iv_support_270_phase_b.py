"""Tier 2: MCP iv support (issue #270 Phase B).

Pre-#270 Phase B: Reyn-as-MCP-server's ``send_to_agent`` ran skills
that could emit ``UserIntervention`` ivs, but the MCP transport had
no chain-override observer registered → iv landed in ChatSession's
``_active`` queue and would hang if no TUI was simultaneously attached
(= the typical MCP-only deployment). The peer also had no way to
answer.

This file pins the Phase B wire (= MCP-side mirror of the A2A α path
PR #300 established):

  1. ``_MCPInterventionBus`` exists and exposes the α observer shape:
     ``on_dispatch`` only, no ``request`` / ``deliver``, no
     ``await iv.future``. Stamps ``iv.origin_channel_id``.
  2. ``_make_mcp_intervention_bus`` builds a bus from the MCP request
     context (= mcp_session + request_id). Returns None when context
     is unavailable (= bypassed-by-test path).
  3. ``_call_tool`` passes the bus as ``intervention_override`` so
     ``send_to_agent_impl`` registers it on the chain.
  4. ``on_dispatch`` pushes the canonical input-required payload via
     ``send_progress_notification`` with JSON-encoded message (= same
     shape PR #285 Gap 4 standardised).
  5. New MCP tool ``answer_intervention`` accepts
     ``{agent_name, run_id, text, choice_id?}`` and routes to
     ``ChatSession.answer_pending_intervention``.
  6. Experimental capability ``reyn.iv.input_required`` declared in
     the ``initialize`` response (= mirrors PR #284's calibration
     pattern: declared capability ↔ in-source wire AST pin).
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed")


# ── 1. _MCPInterventionBus α observer shape ──────────────────────────


def test_mcp_intervention_bus_exposes_on_dispatch_not_request_or_deliver() -> None:
    """Tier 2: α contract — ``on_dispatch`` only. No ``request`` /
    ``deliver`` (= pre-α names that returned an answer).
    """
    from reyn.mcp_server import _MCPInterventionBus

    bus = _MCPInterventionBus(
        mcp_session=None, related_request_id="rq-1",
    )
    assert hasattr(bus, "on_dispatch")
    assert not hasattr(bus, "request")
    assert not hasattr(bus, "deliver")


def test_mcp_intervention_bus_channel_id_format() -> None:
    """Tier 2: ``channel_id`` is ``mcp:<request_id>`` — same prefix
    convention as A2A's ``a2a:<run_id>`` (issue #268 origin-pin
    routing).
    """
    from reyn.mcp_server import _MCPInterventionBus

    bus = _MCPInterventionBus(
        mcp_session=None, related_request_id="rq-42",
    )
    assert bus.channel_id == "mcp:rq-42"


# ── 2. on_dispatch stamps + emits, never awaits future ───────────────


def test_on_dispatch_stamps_origin_channel_id() -> None:
    """Tier 2: ``on_dispatch`` stamps ``iv.origin_channel_id`` when
    unset (= same contract A2AInterventionBus follows for #268).
    """
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import UserIntervention

    class _FakeSession:
        async def send_progress_notification(self, **kwargs):  # noqa: ANN202
            return None

    bus = _MCPInterventionBus(
        mcp_session=_FakeSession(), related_request_id="rq-1",
    )

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?", run_id="run-1")
        await bus.on_dispatch(iv)
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "mcp:rq-1"


def test_on_dispatch_respects_preexisting_origin() -> None:
    """Tier 2: pre-set origin_channel_id is NOT overwritten."""
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import UserIntervention

    class _FakeSession:
        async def send_progress_notification(self, **kwargs):  # noqa: ANN202
            return None

    bus = _MCPInterventionBus(
        mcp_session=_FakeSession(), related_request_id="rq-1",
    )

    async def _drive() -> str | None:
        iv = UserIntervention(
            kind="ask_user", prompt="?",
            run_id="run-1", origin_channel_id="upstream:prior",
        )
        await bus.on_dispatch(iv)
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "upstream:prior"


def test_on_dispatch_returns_without_awaiting_future() -> None:
    """Tier 2: α contract — ``on_dispatch`` returns promptly without
    awaiting ``iv.future``. The handler awaits on the skill's behalf.
    """
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import UserIntervention

    class _FakeSession:
        async def send_progress_notification(self, **kwargs):  # noqa: ANN202
            return None

    bus = _MCPInterventionBus(
        mcp_session=_FakeSession(), related_request_id="rq-1",
    )

    async def _drive() -> bool:
        iv = UserIntervention(kind="ask_user", prompt="?", run_id="run-1")
        # If on_dispatch awaited iv.future this would hang.
        await asyncio.wait_for(bus.on_dispatch(iv), timeout=2.0)
        return not iv.future.done()

    assert asyncio.run(_drive())


# ── 3. Progress notification payload shape ───────────────────────────


def test_on_dispatch_emits_canonical_payload() -> None:
    """Tier 2: ``on_dispatch`` calls ``send_progress_notification``
    with the canonical input-required JSON payload (= Gap 4 shape
    with kind / choices / detail).
    """
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import InterventionChoice, UserIntervention

    captured: list[dict] = []

    class _CapturingSession:
        async def send_progress_notification(
            self, *, progress_token, progress, total, message,
            related_request_id,
        ):
            captured.append({
                "progress_token": progress_token,
                "progress": progress,
                "total": total,
                "message": message,
                "related_request_id": related_request_id,
            })

    bus = _MCPInterventionBus(
        mcp_session=_CapturingSession(), related_request_id="rq-1",
    )

    async def _drive() -> None:
        iv = UserIntervention(
            kind="permission.confirm",
            prompt="Allow read?",
            detail="Path: ~/secrets",
            choices=[
                InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
                InterventionChoice(id="no", label="[N]o", hotkey="n"),
            ],
            run_id="run-1",
        )
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    call = captured[0]
    assert call["progress"] == 0.0  # indeterminate
    assert call["total"] is None
    assert call["related_request_id"] == "rq-1"
    payload = json.loads(call["message"])
    assert payload["type"] == "intervention"
    assert payload["status"] == "input-required"
    assert payload["run_id"] == "run-1"
    assert payload["kind"] == "permission.confirm"
    assert payload["question"] == "Allow read?"
    assert payload["detail"] == "Path: ~/secrets"
    assert payload["choices"] == [
        {"id": "yes", "label": "[Y]es", "hotkey": "y"},
        {"id": "no", "label": "[N]o", "hotkey": "n"},
    ]


def test_on_dispatch_payload_omits_detail_when_empty() -> None:
    """Tier 2: empty ``iv.detail`` is omitted from the payload (=
    Gap 4 contract preserved on MCP side).
    """
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import UserIntervention

    captured: list[str] = []

    class _CapturingSession:
        async def send_progress_notification(
            self, *, progress_token, progress, total, message,
            related_request_id,
        ):
            captured.append(message)

    bus = _MCPInterventionBus(
        mcp_session=_CapturingSession(), related_request_id="rq-2",
    )

    async def _drive() -> None:
        iv = UserIntervention(
            kind="ask_user", prompt="?", run_id="run-1", detail="",
        )
        await bus.on_dispatch(iv)

    asyncio.run(_drive())

    payload = json.loads(captured[0])
    assert "detail" not in payload
    assert payload["choices"] == []
    assert payload["kind"] == "ask_user"


def test_on_dispatch_swallows_send_failure() -> None:
    """Tier 2: when ``send_progress_notification`` raises, ``on_dispatch``
    returns cleanly (= side-effect failure must not block dispatch).
    """
    from reyn.mcp_server import _MCPInterventionBus
    from reyn.user_intervention import UserIntervention

    class _FailingSession:
        async def send_progress_notification(self, **kwargs):  # noqa: ANN202
            raise RuntimeError("simulated MCP transport failure")

    bus = _MCPInterventionBus(
        mcp_session=_FailingSession(), related_request_id="rq-1",
    )

    async def _drive() -> None:
        iv = UserIntervention(kind="ask_user", prompt="?", run_id="run-1")
        await bus.on_dispatch(iv)

    # No exception escapes.
    asyncio.run(_drive())


# ── 4. _call_tool wires the bus + send_to_agent_impl forwards it ─────


def test_call_tool_send_to_agent_passes_iv_bus_as_override() -> None:
    """Tier 2: AST grep — the send_to_agent branch of _call_tool MUST
    construct ``_make_mcp_intervention_bus`` AND pass it as
    ``intervention_override`` to ``send_to_agent_impl``. Pin the
    wiring so a refactor that drops it fails first.
    """
    from reyn import mcp_server

    src = inspect.getsource(mcp_server.build_server)
    assert "_make_mcp_intervention_bus" in src
    assert "intervention_override=iv_bus" in src


# ── 5. answer_intervention tool exists and routes correctly ──────────


def test_answer_intervention_tool_is_declared() -> None:
    """Tier 2: the ``answer_intervention`` tool is part of the MCP
    server's tool list with the canonical input schema (=
    ``agent_name`` / ``run_id`` / ``text`` required, ``choice_id``
    optional).
    """
    from reyn import mcp_server

    src = inspect.getsource(mcp_server.build_server)
    # The tool definition must appear in build_server's source.
    assert 'name="answer_intervention"' in src
    assert "ChatSession.answer_pending_intervention" in src
    # Schema sanity: required fields are listed.
    assert '"required": ["agent_name", "run_id", "text"]' in src


# ── 6. Experimental capability declared ──────────────────────────────


def test_serve_stdio_declares_iv_input_required_capability() -> None:
    """Tier 2: ``serve_stdio`` declares ``reyn.iv.input_required`` in
    the experimental capabilities of the ``initialize`` response (=
    PR #284's claim/wire calibration pattern: pin both sides of the
    contract so future refactors that drop one without the other
    fail first).
    """
    from reyn import mcp_server

    src = inspect.getsource(mcp_server.serve_stdio)
    assert '"reyn.iv.input_required"' in src
    assert '"answer_tool": "answer_intervention"' in src


def test_iv_input_required_capability_backed_by_in_source_wire() -> None:
    """Tier 2: every declared experimental capability must derive from
    a concrete in-source wire (= avoid the #267 Z-b "claim/reality
    mismatch" pattern). The ``reyn.iv.input_required`` claim is
    backed by ``_MCPInterventionBus.on_dispatch`` + the
    ``answer_intervention`` tool dispatch. Pin both wires.
    """
    src_path = (  # #1682: impl moved to reyn/mcp/server.py (old path = shim)
        Path(__file__).parent.parent
        / "src" / "reyn" / "mcp" / "server.py"
    )
    src = src_path.read_text(encoding="utf-8")

    # The observer wire.
    assert "class _MCPInterventionBus" in src
    assert "send_progress_notification" in src

    # The answer-injection wire.
    assert 'name == "answer_intervention"' in src
    assert "answer_pending_intervention" in src
