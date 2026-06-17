"""Tier 2: MCP server ``resources/read`` adapter (#385 β core impl
sub-task 3d).

The MCP server side of cross-protocol path_ref fetch: external MCP
clients (= Claude Desktop, Cursor, ...) receive a path_ref in a tool
response containing a ``reyn-tool-result://<agent>/<artifact>`` URI
and call ``resources/read`` to fetch the body. This file pins:

1. The handler resolves a valid ``reyn-tool-result://`` URI to the
   minted body content (= round-trip with MediaStore.save_tool_result).
2. An unsupported URI scheme returns a structured error string (= no
   crash, no leak).
3. A valid URI for a missing file returns a structured not-found error.
4. Path-traversal protection inherited from MediaStore (= boundary
   check prevents reads outside ``.reyn/tool-results/``).

Tier 2 because the route is the cross-protocol integrity boundary and
matches the same-host transport contract that ``read_tool_result_by_uri``
already pins (= consistent error semantics across MCP and Reyn-native
dispatch).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed")

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.core.events.state_log import StateLog
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.mcp_server import build_server


def _build_registry_with_agent(tmp_path: Path, agent_name: str) -> AgentRegistry:
    """Minimal AgentRegistry with one agent under tmp_path."""
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    registry.create(agent_name, role="")
    return registry


def _invoke_read_resource(server, uri_str: str):
    """Drive the SDK-registered ``resources/read`` handler synchronously.

    The MCP SDK keys handlers by request type in ``request_handlers``;
    construct the request explicitly and run the coroutine. Returns the
    handler's wrapped result root (= a ``ReadResourceResult`` whose
    ``contents`` carries the body).
    """
    from mcp.types import ReadResourceRequest, ReadResourceRequestParams
    handler = server.request_handlers[ReadResourceRequest]
    req = ReadResourceRequest(
        method="resources/read",
        params=ReadResourceRequestParams(uri=uri_str),
    )
    return asyncio.run(handler(req))


# ── happy path ─────────────────────────────────────────────────────────


def test_read_resource_returns_body_for_minted_path_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: a path-ref minted by MediaStore round-trips through the
    MCP server's ``resources/read`` handler (= external client view).

    Mints a real ``reyn-tool-result://`` URI via MediaStore (same
    machinery the production tool path uses), passes it to the handler,
    expects the original body back. Confirms the cross-protocol
    contract: external MCP client → ``resources/read`` → local fs.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry_with_agent(tmp_path, "researcher")
    server = build_server(registry)

    store = MediaStore(
        MediaStoreConfig(),
        project_root=tmp_path,
        agent_name="researcher",
    )
    block = store.save_tool_result(
        "mcp body\n", mime_type="text/plain",
        chain_id="c1", tool="web_fetch", seq=1,
    )
    uri = block["resource_uri"]

    result = _invoke_read_resource(server, uri)

    # ReadResourceResult.contents is a list of resource contents items.
    contents = result.root.contents
    assert len(contents) >= 1
    # Single-text-content body — verify the text round-trips.
    item = contents[0]
    assert getattr(item, "text", None) == "mcp body\n"


# ── error / edge cases ────────────────────────────────────────────────


def test_read_resource_returns_error_for_unsupported_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: a URI with a scheme the handler doesn't understand (=
    ``file://`` / arbitrary HTTP URLs) yields a structured error string
    rather than crashing the server or attempting an arbitrary read.

    The MCP SDK wraps the str return as a TextResourceContents; the
    client sees the error message and can correct its request.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry_with_agent(tmp_path, "researcher")
    server = build_server(registry)

    result = _invoke_read_resource(server, "file:///etc/passwd")

    contents = result.root.contents
    text = getattr(contents[0], "text", "")
    assert "error" in text.lower()
    assert "unsupported" in text.lower() or "scheme" in text.lower()


def test_read_resource_returns_error_for_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: a syntactically valid ``reyn-tool-result://`` URI whose
    backing file doesn't exist surfaces a structured "not found" error
    string rather than crashing — matches the not_found semantics that
    ``read_tool_result`` / ``read_tool_result_by_uri`` already provide.
    """
    monkeypatch.chdir(tmp_path)
    registry = _build_registry_with_agent(tmp_path, "researcher")
    server = build_server(registry)

    # Mint the agent dir but no file at the URI's artifact path.
    (tmp_path / ".reyn" / "tool-results").mkdir(parents=True, exist_ok=True)
    uri = "reyn-tool-result://researcher/20990101T000000-none-tool-1.txt"

    result = _invoke_read_resource(server, uri)

    text = getattr(result.root.contents[0], "text", "")
    assert "error" in text.lower()
    assert "not found" in text.lower() or "never existed" in text.lower()
