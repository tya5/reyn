"""Tier 2: #2336 follow-up — an MCP op result never re-carries its content in a duplicate field.

The op result used to carry ``raw`` (the full flattened CallToolResult), which re-carried the same
oversized ``content`` text — a duplicate field that (on the now-retired DICT-offload path, #2396
Step 4) defeated the clean-payload gate and produced a whole-dict single-line offload envelope.
webfetch was unaffected only because ``content`` was its sole large field.

Fix (op-side ``mcp.py`` only, P7-safe): drop ``raw`` (``isError`` is already ``status``, the joined
text is already ``content``), and preserve the only non-duplicate SDK field — ``structuredContent``
— as ``structured`` ONLY when present. Real ``_execute`` via a stubbed MCP client (no subprocess).

These tests pin the op-level shape fact directly (no ``raw`` duplicate field; ``structured`` present
iff the SDK returned one) rather than through the retired offload-decision helpers. The canonical
mapper's own handling of ``content``/``structured`` (text vs. attachment) is covered separately by
``tests/test_2425_canonical_tool_result.py``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class _StubPool:
    """Test double for MCPClientPool — get() returns a pre-set client (a359 P2). Real Fake."""
    def __init__(self, client): self._client = client
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    @property
    def owner_task(self): return None
    async def get(self, server, config, *, agent_id=None): return self._client


class _FakeMCPClient:
    """Stand-in for ``reyn.mcp.client.MCPClient`` — returns a canned ``call_tool`` result (the
    flattened ``{content, isError, structuredContent}`` shape) without spawning a subprocess."""

    def __init__(self, content: list[dict], *, is_error: bool = False, structured: Any = None) -> None:
        self._content = content
        self._is_error = is_error
        self._structured = structured

    async def call_tool(self, name: str, args: dict, *, progress_callback=None, timeout_seconds=None) -> dict:
        return {"content": self._content, "isError": self._is_error, "structuredContent": self._structured}


def _make_ctx(tmp_path: Path, mcp_client: _FakeMCPClient) -> Any:
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    return OpContext(
        workspace=Workspace(events=events),
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,  # bypass permission gate
        mcp_servers={"testsrv": {"type": "stdio", "command": "fake"}},
        mcp_pool=_StubPool(mcp_client),  # #a359 P2
    )


def _run(content: list[dict], tmp_path: Path, **kw) -> dict:
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    ctx = _make_ctx(tmp_path, _FakeMCPClient(content, **kw))
    op = MCPIROp(kind="mcp", server="testsrv", tool="dump", args={})
    return asyncio.run(_execute(op, ctx))


def test_no_raw_field_duplicating_content(tmp_path, monkeypatch):
    """Tier 2: CORE — an MCP result never carries a ``raw`` field that duplicates ``content``."""
    monkeypatch.chdir(tmp_path)
    big_text = "\n".join(f"row {i}: " + "d" * 80 for i in range(400))
    result = _run([{"type": "text", "text": big_text}], tmp_path)

    assert "raw" not in result, "the content-duplicating `raw` field is dropped"
    assert result["content"] == big_text


def test_structured_content_preserved_when_present(tmp_path, monkeypatch):
    """Tier 2: a real MCP structured output is preserved as ``structured`` (no in-context data loss),
    and it does not re-carry ``content``."""
    monkeypatch.chdir(tmp_path)
    structured = {"rows": [1, 2, 3], "schema": "v1"}
    result = _run([{"type": "text", "text": "hi"}], tmp_path, structured=structured)

    assert result["structured"] == structured, "structuredContent preserved as `structured`"
    assert "raw" not in result


def test_structured_absent_when_none(tmp_path, monkeypatch):
    """Tier 2: when the tool returns no structured output (the default), there is NO ``structured``
    field — clean end-state, no shim key."""
    monkeypatch.chdir(tmp_path)
    result = _run([{"type": "text", "text": "small"}], tmp_path)  # structured defaults to None

    assert "structured" not in result, "no `structured` field when structuredContent is None"
    assert "raw" not in result
