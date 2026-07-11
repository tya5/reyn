"""Tier 2: ignore-unknown holds in BOTH directions (ADR-0039 P4, D6).

D6's honest ceiling becomes a stated + tested floor: an unknown input is skipped,
never fatal, on both sides of the wire.

Client (decode):
  (a) a foreign standard event with no ``_reyn`` block → ``None``;
  (b) an event whose ``_reyn`` block carries an unknown ``frame`` tag → ``None``;
  (c) SR3: an unknown ``reyn.*`` Custom NAME → skip (``None``), not crash.

Server (POST dispatch):
  an unknown input type graceful-degrades — no ``KeyError`` / 500 — and does not
  swallow or break the REAL dispatch surface: after an unknown verb, a turn
  (``user_message``) and an answer (``TOOL_CALL_RESULT``, BY-ID) still route to
  their handlers and the ``/seize`` route degrades cleanly.

Real instances only — the real codec, a real FastAPI app mounting the real
router with a real AuthContext + a hand-written real registry Fake; no mocks.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reyn.interfaces.transport.agui import endpoint as endpoint_mod
from reyn.interfaces.transport.agui.endpoint import router
from reyn.interfaces.transport.agui.protocol import decode_event
from reyn.interfaces.web.auth import AuthContext

# ── client side: decode ignore-unknown (a) / (b) / (c) ──────────────────────

def test_client_skips_foreign_event_without_reyn_block() -> None:
    """Tier 2: (a) a standard event with no _reyn reconstruction block → None."""
    assert decode_event("TEXT_MESSAGE_START", {"messageId": "m1", "role": "assistant"}) is None
    assert decode_event("TOOL_CALL_END", {"toolName": "x", "status": "ok"}) is None


def test_client_skips_unknown_reyn_frame_tag() -> None:
    """Tier 2: (b) a _reyn block with a frame tag this client predates → None."""
    assert decode_event("CUSTOM", {"_reyn": {"frame": "some_future_frame_kind"}}) is None


def test_client_skips_unknown_reyn_custom_name() -> None:
    """Tier 2: (c/SR3) an unknown reyn.* Custom NAME is skipped, not a crash."""
    # No _reyn block (a future Custom a generic-shaped reyn event might carry).
    assert decode_event("CUSTOM", {"name": "reyn.future.widget", "value": {"a": 1}}) is None
    # A _reyn block whose frame tag is unknown is likewise skipped, not fatal.
    assert decode_event("CUSTOM", {"name": "reyn.future.widget", "_reyn": {"frame": "widget"}}) is None


# ── server side: POST dispatch graceful-degrade against the REAL dispatch ────
# The endpoint dispatches several verbs (user_message / TOOL_CALL_RESULT answer /
# cancel / heartbeat) plus a separate /seize route. An unknown verb must
# graceful-degrade WITHOUT swallowing or breaking any of the real ones.

class _FakeSession:
    """A minimal real session Fake: records the turn/answer verbs it receives."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.answers: list[str] = []

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_by_id(self, iv_id: str, text: str, **kw) -> bool:
        self.answers.append(iv_id)
        return True  # the id matched a pending intervention


class _FakeRegistry:
    """A minimal real registry Fake exposing the endpoint's touch-points."""

    def __init__(self) -> None:
        self.session = _FakeSession()

    def exists(self, name: str) -> bool:
        return True

    async def attach(self, name: str):
        return self.session


def _app(monkeypatch, registry: _FakeRegistry) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    # Swap the module-level registry accessor for a real hand-written Fake.
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: registry)
    return app


def test_server_unknown_input_type_graceful_degrades(monkeypatch) -> None:
    """Tier 2: an unknown POST input type does NOT 500 / KeyError — it falls
    through to a clean 200 ack (server-side ignore-unknown), driving nothing."""
    registry = _FakeRegistry()
    client = TestClient(_app(monkeypatch, registry))

    resp = client.post(
        "/agui/chat/demo?token=s3cret",
        json={"type": "some_future_input_type", "payload": {"k": "v"}},
    )
    assert resp.status_code == 200
    # Graceful no-op: neither a turn nor an answer was driven.
    assert registry.session.submitted == []
    assert registry.session.answers == []


def test_server_real_dispatch_surface_intact_around_unknown(monkeypatch) -> None:
    """Tier 2: ignore-unknown does not swallow or break the real dispatch — after
    an unknown verb, both a turn (user_message) and an answer (TOOL_CALL_RESULT)
    still route to their handlers, and the /seize route degrades without a 500."""
    registry = _FakeRegistry()
    client = TestClient(_app(monkeypatch, registry))

    # Unknown verb first (graceful), then the real verbs must still land.
    assert client.post("/agui/chat/demo?token=s3cret", json={"type": "nope"}).status_code == 200

    turn = client.post(
        "/agui/chat/demo?token=s3cret", json={"type": "user_message", "text": "hello"}
    )
    assert turn.status_code == 200
    assert registry.session.submitted == ["hello"]

    # TOOL_CALL_RESULT routes to the answer path (BY-ID), not treated as unknown.
    answer = client.post(
        "/agui/chat/demo?token=s3cret",
        json={"type": "TOOL_CALL_RESULT", "toolCallId": "iv-7", "text": "yes"},
    )
    assert answer.status_code == 200
    assert answer.json()["answered"] is True
    assert registry.session.answers == ["iv-7"]

    # The seize route exists and degrades cleanly (no surface attached → 409
    # "seize refused"), never a 500.
    seize = client.post("/agui/chat/demo/seize?token=s3cret")
    assert seize.status_code != 500
