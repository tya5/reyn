"""Tier 2: A2A (Agent2Agent) protocol surface on the FastAPI gateway.

Pins the contract that Reyn exposes each agent at the canonical A2A
discovery URL (``.well-known/agent-card.json``) and that the JSON-RPC
``message/send`` method backs onto the same ``send_to_agent_impl`` the
MCP path uses (= one source of truth for cross-protocol peer chat).

We exercise the FastAPI surface end-to-end through ``TestClient`` but
patch ``router_loop.call_llm_tools`` so the agent emits a deterministic
reply — same fake-LLM pattern as ``test_mcp_server.py``.

Out of scope (= tracked as A2A v2 follow-ups):
  * ``message/stream`` — streaming SSE responses.
  * Task lifecycle (``tasks/get``, ``tasks/cancel``, ``tasks/pushNotificationConfig/*``).
  * Authentication / agent capability negotiation beyond the static card.
  * Non-text message parts (``file``, ``data``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the worktree src is importable.
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# Skip the whole module if optional deps are missing.
pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


from reyn.budget.budget import BudgetTracker, CostConfig  # noqa: E402
from reyn.chat.profile import AgentProfile  # noqa: E402
from reyn.chat.registry import AgentRegistry  # noqa: E402
from reyn.chat.session import ChatSession  # noqa: E402
from reyn.events.state_log import StateLog  # noqa: E402
from reyn.llm.llm import LLMToolCallResult  # noqa: E402
from reyn.llm.pricing import TokenUsage  # noqa: E402

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _build_registry(tmp_path: Path, agents: list[tuple[str, str]]) -> AgentRegistry:
    """Construct an AgentRegistry on tmp_path with the given (name, role) agents."""
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )

    for name, role in agents:
        if name == "default":
            agent_dir = registry._dir / name
            AgentProfile.new(name, role=role).save(agent_dir)
        else:
            registry.create(name, role=role)

    return registry


def _client_with_registry(tmp_path: Path, agents: list[tuple[str, str]]):
    """Construct a TestClient that uses a tmp_path-backed registry.

    Overrides the ``get_registry`` and ``get_run_registry`` dependencies
    so we don't touch the process-wide singletons from ``deps.py``
    (= FP-0009 B moved RunRegistry init into the FastAPI lifespan;
    TestClient without ``with ...`` doesn't fire lifespan).
    """
    from fastapi.testclient import TestClient

    from reyn.web.deps import get_registry, get_run_registry
    from reyn.web.run_registry import RunRegistry
    from reyn.web.server import app

    registry = _build_registry(tmp_path, agents)
    run_registry = RunRegistry()
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    client = TestClient(app, raise_server_exceptions=False)
    return client, registry


def _restore_overrides() -> None:
    """Clear FastAPI dependency overrides between tests."""
    from reyn.web.server import app
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Routes are mounted
# ---------------------------------------------------------------------------


def test_a2a_routes_mounted() -> None:
    """Tier 2: the A2A router exposes the three documented routes.

    Pins the wiring without exercising the protocol. A renamed router
    or a missing ``include_router`` would trip this test.
    """
    from reyn.web.server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/a2a/agents" in paths
    assert "/a2a/agents/{agent_name}/.well-known/agent-card.json" in paths
    assert "/a2a/agents/{agent_name}" in paths


# ---------------------------------------------------------------------------
# 2. Agent Card discovery
# ---------------------------------------------------------------------------


def test_agent_card_returns_canonical_shape(tmp_path):
    """Tier 2: GET /a2a/agents/{name}/.well-known/agent-card.json
    returns an A2A-compliant Agent Card with the agent's role as
    description and the JSON-RPC endpoint URL, and FP-0001 capabilities
    (streaming=True, pushNotifications=True).
    """
    client, _ = _client_with_registry(tmp_path, [("default", "general assistant")])
    try:
        r = client.get("/a2a/agents/default/.well-known/agent-card.json")
        assert r.status_code == 200, r.text
        card = r.json()

        # Identity & description: P7-safe — role flows through opaquely.
        assert card["name"] == "default"
        assert card["description"] == "general assistant"

        # Endpoint URL points at the JSON-RPC handler for THIS agent.
        assert card["url"].endswith("/a2a/agents/default")

        # Capabilities advertised: streaming + push are True (FP-0001).
        caps = card["capabilities"]
        assert caps["streaming"] is True
        assert caps["pushNotifications"] is True

        # Modes + skills shape (peers parse these to capability-negotiate).
        assert "text/plain" in card["defaultInputModes"]
        assert "text/plain" in card["defaultOutputModes"]
        assert isinstance(card["skills"], list) and len(card["skills"]) >= 1
        assert card["skills"][0]["id"] == "chat"
    finally:
        _restore_overrides()


def test_agent_card_unknown_agent_returns_404(tmp_path):
    """Tier 2: card request for a non-existent agent is 404 (not 500).

    Pins that the existence check happens before the card body is
    constructed — a peer enumerating cards must be able to distinguish
    "agent doesn't exist" from "Reyn server is broken".
    """
    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        r = client.get("/a2a/agents/ghost/.well-known/agent-card.json")
        assert r.status_code == 404, r.text
    finally:
        _restore_overrides()


def test_list_a2a_agents_enumerates_registered(tmp_path):
    """Tier 2: GET /a2a/agents returns one entry per registered agent
    with the URL the peer should fetch the card from.

    Convenience endpoint (= not in A2A spec proper) for peers that want
    to discover what's available before requesting individual cards.
    """
    client, _ = _client_with_registry(
        tmp_path,
        [("default", "general"), ("planner", "plans"), ("coder", "writes code")],
    )
    try:
        r = client.get("/a2a/agents")
        assert r.status_code == 200, r.text
        body = r.json()
        names = {a["name"] for a in body["agents"]}
        assert names == {"default", "planner", "coder"}
        for a in body["agents"]:
            assert a["agentCardUrl"].endswith(
                f"/a2a/agents/{a['name']}/.well-known/agent-card.json",
            )
            assert a["endpoint"].endswith(f"/a2a/agents/{a['name']}")
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 3. JSON-RPC: happy path
# ---------------------------------------------------------------------------


def test_message_send_returns_a2a_message(tmp_path, monkeypatch):
    """Tier 2: POST /a2a/agents/default with method=message/send returns
    a JSON-RPC success envelope whose result is an A2A Message wrapping
    the agent's reply text.

    Pins (a) the JSON-RPC framing, (b) the A2A Message shape (role,
    parts, kind), and (c) end-to-end backing onto send_to_agent_impl.
    """
    monkeypatch.chdir(tmp_path)

    async def fake_llm_tools(**kw):
        return _text_result("Hi from Reyn via A2A!")

    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        with patch("reyn.chat.router_loop.call_llm_tools", side_effect=fake_llm_tools):
            r = client.post(
                "/a2a/agents/default",
                json={
                    "jsonrpc": "2.0",
                    "id": "req-1",
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "Hello"}],
                            "messageId": "msg-1",
                        },
                    },
                },
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == "req-1"
        assert "error" not in body

        result = body["result"]
        assert result["kind"] == "message"
        assert result["role"] == "agent"
        assert isinstance(result["parts"], list) and len(result["parts"]) >= 1
        assert result["parts"][0]["kind"] == "text"
        assert "Hi from Reyn via A2A!" in result["parts"][0]["text"]
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 4. JSON-RPC: error paths
# ---------------------------------------------------------------------------


def test_message_send_unknown_agent_returns_jsonrpc_error(tmp_path):
    """Tier 2: posting to an unknown agent yields HTTP 200 + JSON-RPC
    error -32602 (Invalid params), NOT HTTP 404.

    A2A peers parse the JSON-RPC envelope; a 4xx HTTP response would
    short-circuit that. Also pins that ``ValueError`` from the backing
    impl is caught and translated rather than leaking as 500.
    """
    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        r = client.post(
            "/a2a/agents/ghost",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hi"}],
                    },
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == 7
        assert "error" in body
        assert body["error"]["code"] == -32602
        assert "ghost" in body["error"]["message"].lower()
    finally:
        _restore_overrides()


def test_unsupported_method_returns_method_not_found(tmp_path):
    """Tier 2: an unsupported A2A method (e.g. message/stream) returns
    JSON-RPC -32601 ``Method not found``.

    Lets peers capability-fall-back gracefully instead of guessing
    whether the absence of streaming means "not supported" or
    "transport broken".
    """
    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        r = client.post(
            "/a2a/agents/default",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/stream",
                "params": {},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["error"]["code"] == -32601
        assert "message/stream" in body["error"]["message"]
    finally:
        _restore_overrides()


def test_invalid_jsonrpc_envelope_returns_invalid_request(tmp_path):
    """Tier 2: a body missing ``jsonrpc: "2.0"`` returns -32600
    Invalid Request, not 5xx.

    Three sub-cases collapsed into one parameterless test (each calls
    `client.post` once): missing jsonrpc field, wrong jsonrpc version,
    method not a string. All trip the same error path.
    """
    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        # Missing jsonrpc field.
        r = client.post(
            "/a2a/agents/default",
            json={"id": 1, "method": "message/send", "params": {}},
        )
        assert r.json()["error"]["code"] == -32600

        # Wrong version.
        r = client.post(
            "/a2a/agents/default",
            json={"jsonrpc": "1.0", "id": 1, "method": "message/send", "params": {}},
        )
        assert r.json()["error"]["code"] == -32600

        # Method not a string.
        r = client.post(
            "/a2a/agents/default",
            json={"jsonrpc": "2.0", "id": 1, "method": 42, "params": {}},
        )
        assert r.json()["error"]["code"] == -32600
    finally:
        _restore_overrides()


def test_message_with_empty_parts_returns_invalid_params(tmp_path):
    """Tier 2: ``params.message.parts`` empty / missing / all-non-text
    returns -32602 Invalid params.

    Pins that the part-extraction layer fails fast before reaching the
    agent — sending the agent an empty user message would produce
    spurious LLM calls and possibly unhelpful replies.
    """
    client, _ = _client_with_registry(tmp_path, [("default", "")])
    try:
        # Empty parts list.
        r = client.post(
            "/a2a/agents/default",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"role": "user", "parts": []}},
            },
        )
        assert r.json()["error"]["code"] == -32602

        # Only a non-text part (file part — not yet supported).
        r = client.post(
            "/a2a/agents/default",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "file", "file": {"uri": "x"}}],
                    },
                },
            },
        )
        assert r.json()["error"]["code"] == -32602

        # All-whitespace text part.
        r = client.post(
            "/a2a/agents/default",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "   "}],
                    },
                },
            },
        )
        assert r.json()["error"]["code"] == -32602
    finally:
        _restore_overrides()
