"""Tier 2: MCP image content → multimodal LLM message (issue #362).

The chat router needs to forward MCP-returned images to vision-capable
LLMs. The flow has two layers we pin here:

  1. op_runtime/mcp.py — preserves image content blocks in the op result
     under `media_blocks`, alongside the text-only `content` summary.
  2. chat/router_loop._build_media_followup_message — converts a list of
     MCP image blocks into a litellm-normalised user message with
     data-URL image_url parts.

Real instances, no collaborator mocks. The MCP server is stubbed via a
minimal client; the LLM layer is not invoked.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.chat.router_loop import _build_media_followup_message

# ── op_runtime/mcp.py: media_blocks preservation ───────────────────────


class _FakeMCPClient:
    """Stand-in for ``reyn.mcp_client.MCPClient`` — returns a canned
    ``call_tool`` result without spawning a subprocess.
    """

    def __init__(self, content: list[dict], *, is_error: bool = False) -> None:
        self._content = content
        self._is_error = is_error

    async def call_tool(
        self, name: str, args: dict, *,
        progress_callback: Any = None, timeout_seconds: Any = None,
    ) -> dict:
        return {
            "content": self._content,
            "isError": self._is_error,
            "structuredContent": None,
        }


def _make_ctx(tmp_path, mcp_client: _FakeMCPClient) -> Any:
    """Build a minimal OpContext that uses the fake client for the test server."""
    from reyn.events.events import EventLog
    from reyn.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.workspace.workspace import Workspace

    events = EventLog()
    return OpContext(
        workspace=Workspace(events=events),
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,  # bypass permission gate
        mcp_servers={"testsrv": {"type": "stdio", "command": "fake"}},
        mcp_clients={"testsrv": mcp_client},  # type: ignore[dict-item]
    )


def test_op_result_preserves_image_blocks(tmp_path, monkeypatch):
    """Tier 2: MCP result with image content → op result's media_blocks
    carries the image block; text content remains the text-only join.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    image_block = {
        "type": "image",
        "data": "iVBORw0KGgoAAAANSU=",  # truncated base64 sample
        "mimeType": "image/png",
    }
    text_block = {"type": "text", "text": "screenshot taken"}
    client = _FakeMCPClient(content=[text_block, image_block])
    ctx = _make_ctx(tmp_path, client)

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshot", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "screenshot taken"
    assert result["media_blocks"] == [image_block]


def test_op_result_text_only_keeps_empty_media_blocks(tmp_path, monkeypatch):
    """Tier 2: text-only MCP result → media_blocks is an empty list (=
    backward compat for callers that only read `content`).
    """
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    client = _FakeMCPClient(content=[{"type": "text", "text": "hello"}])
    ctx = _make_ctx(tmp_path, client)

    op = MCPIROp(kind="mcp", server="testsrv", tool="echo", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["content"] == "hello"
    assert result["media_blocks"] == []


def test_op_result_multiple_images_all_preserved(tmp_path, monkeypatch):
    """Tier 2: MCP result with multiple image blocks → all preserved in order."""
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    img1 = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    img2 = {"type": "image", "data": "BBBB", "mimeType": "image/jpeg"}
    client = _FakeMCPClient(content=[img1, img2])
    ctx = _make_ctx(tmp_path, client)

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshots", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["media_blocks"] == [img1, img2]
    assert result["content"] == ""  # no text blocks


# ── _build_media_followup_message: shape ───────────────────────────────


def test_followup_builds_litellm_image_url_format() -> None:
    """Tier 2: image blocks → user message with image_url parts in
    data-URL form (= litellm / OpenAI-vision standard wire format).
    """
    blocks = [
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
    ]
    msg = _build_media_followup_message(tool_name="screenshot", media_blocks=blocks)

    assert msg is not None
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    assert msg["content"][0]["type"] == "text"
    assert "screenshot" in msg["content"][0]["text"]
    image_part = msg["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_followup_defaults_mime_type_to_png() -> None:
    """Tier 2: when an image block omits mimeType, default to image/png."""
    blocks = [{"type": "image", "data": "XYZ"}]
    msg = _build_media_followup_message(tool_name="t", media_blocks=blocks)

    assert msg is not None
    assert msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_followup_handles_snake_case_mime_type() -> None:
    """Tier 2: MCP servers may serialise mimeType as snake_case mime_type
    (= depending on SDK version); both keys should resolve.
    """
    blocks = [{"type": "image", "data": "XYZ", "mime_type": "image/webp"}]
    msg = _build_media_followup_message(tool_name="t", media_blocks=blocks)

    assert msg is not None
    assert "data:image/webp;base64,XYZ" in msg["content"][1]["image_url"]["url"]


def test_followup_skips_non_image_blocks() -> None:
    """Tier 2: non-image media blocks (= resources, etc.) are filtered
    out for now (deferred until a real use case appears). When ALL blocks
    are skippable, the function returns None so caller doesn't append
    an empty follow-up.
    """
    blocks = [
        {"type": "resource", "resource": {"uri": "file:///x"}},
        {"type": "unknown_kind", "data": "..."},
    ]
    msg = _build_media_followup_message(tool_name="t", media_blocks=blocks)

    assert msg is None


def test_followup_drops_image_block_with_empty_data() -> None:
    """Tier 2: image block with empty / missing data string is dropped
    (= protect against malformed server responses).
    """
    blocks = [
        {"type": "image", "data": "", "mimeType": "image/png"},
        {"type": "image", "mimeType": "image/png"},  # no data key at all
    ]
    msg = _build_media_followup_message(tool_name="t", media_blocks=blocks)

    assert msg is None  # both dropped → no usable blocks → no follow-up


def test_followup_preserves_block_order() -> None:
    """Tier 2: multiple image blocks → image_url parts appear in the
    same order as the input list.
    """
    blocks = [
        {"type": "image", "data": "FIRST", "mimeType": "image/png"},
        {"type": "image", "data": "SECOND", "mimeType": "image/jpeg"},
    ]
    msg = _build_media_followup_message(tool_name="t", media_blocks=blocks)

    assert msg is not None
    urls = [p["image_url"]["url"] for p in msg["content"] if p["type"] == "image_url"]
    assert urls == [
        "data:image/png;base64,FIRST",
        "data:image/jpeg;base64,SECOND",
    ]
