"""Tier 2: #4946 — MCP-sourced images must pass the shared multi-modal
size gate (``PermissionResolver.require_media_load``), the same gate
web_fetch (web.py) / read_file (file.py) / user ``/image`` (image.py)
already apply before persisting any image bytes.

Measured before this fix (issue #4944's Angle 3 byproduct): the gate's
own docstring named MCP as one of its 4 producers, but `require_media_load`
had ZERO call sites reached from ``op_runtime/mcp.py`` — an oversized MCP
image bypassed the 5MB cap entirely.

Gated PER IMAGE (lead-coder's explicit ruling, not a mirrored-without-
thought copy of the other 3 producers' shape): a single MCP tool call can
return several images, unlike the other 3 producers (always exactly one).
Rejecting the whole result for one oversized image among several would
lose correctly-sized images and text unnecessarily.

Real ``PermissionResolver`` + ``OpContext``, no collaborator mocks — same
convention as ``test_web_fetch_binary_media_gate.py`` and
``test_mcp_multimodal_forwarding.py``, which this file is a sibling of.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from reyn.config import MultimodalConfig
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    """Drop-in for RequestBus that pre-answers the prompt with `answer`
    (same shape as test_web_fetch_binary_media_gate.py's own fake)."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text="", choice_id=self._answer)


class _FakeMCPClient:
    """Stand-in for reyn.mcp.client.MCPClient — returns a canned
    call_tool result without spawning a subprocess (mirrors
    test_mcp_multimodal_forwarding.py's own fake)."""

    def __init__(self, content: list[dict], *, is_error: bool = False) -> None:
        self._content = content
        self._is_error = is_error

    async def call_tool(
        self, name: str, args: dict, *,
        progress_callback: Any = None, timeout_seconds: Any = None,
    ) -> dict:
        return {"content": self._content, "isError": self._is_error, "structuredContent": None}


class _StubPool:
    def __init__(self, client) -> None:
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    @property
    def owner_task(self):
        return None

    async def get(self, server, config, *, agent_id=None):
        return self._client


def _make_gated_ctx(
    tmp_path: Path, mcp_client: _FakeMCPClient, *,
    multimodal: MultimodalConfig, bus_answer: str = "yes",
) -> Any:
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace

    events = EventLog()
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)
    return OpContext(
        workspace=Workspace(events=events, permission_resolver=resolver),
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        intervention_bus=_FakeBus(bus_answer),  # type: ignore[arg-type]
        multimodal_config=multimodal,
        mcp_servers={"testsrv": {"type": "stdio", "command": "fake"}},
        mcp_pool=_StubPool(mcp_client),
    )


def _image_block(size_bytes: int, *, mime: str = "image/png") -> dict:
    return {
        "type": "image",
        "data": base64.b64encode(b"x" * size_bytes).decode("ascii"),
        "mimeType": mime,
    }


def test_oversize_mcp_image_is_actually_rejected(tmp_path, monkeypatch) -> None:
    """Tier 2: #4946 acceptance ① — a real oversized (>5MB) MCP image is
    NOT persisted via media_store and does NOT appear in media_blocks.
    "no exception raised" is not the witness — the ABSENCE of the block
    (and the media_store file it would have created) is."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    big_image = _image_block(6_000_000)  # over the 5MB default cap
    client = _FakeMCPClient(content=[big_image])
    ctx = _make_gated_ctx(
        tmp_path, client,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshot", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["status"] == "ok", "the OP itself still succeeds — only the image is dropped"
    assert result["media_blocks"] == [], (
        f"an oversized image must never reach media_blocks; got {result['media_blocks']!r}"
    )


def test_oversized_image_among_normal_ones_drops_only_the_oversized_one(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #4946 acceptance ② — the per-image witness. One oversized
    image among 2 correctly-sized ones + text: the 2 normal images and
    the text must still reach the caller. Rejecting the WHOLE result for
    one oversized image would lose content the gate has no reason to
    touch."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    small_1 = _image_block(1_000)
    big = _image_block(6_000_000)
    small_2 = _image_block(2_000)
    text_block = {"type": "text", "text": "here are the screenshots"}
    client = _FakeMCPClient(content=[text_block, small_1, big, small_2])
    ctx = _make_gated_ctx(
        tmp_path, client,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshots", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["status"] == "ok"
    surviving_data = {b.get("data") for b in result["media_blocks"] if isinstance(b, dict)}
    assert small_1["data"] in surviving_data, "the first correctly-sized image must survive"
    assert small_2["data"] in surviving_data, "the second correctly-sized image must survive"
    assert big["data"] not in surviving_data, "the oversized image must not survive"
    assert "here are the screenshots" in result["content"], (
        "the original text must still reach the caller unchanged"
    )


def test_mcp_media_denied_event_emitted(tmp_path, monkeypatch) -> None:
    """Tier 2: #4946 acceptance ③ — a real audit-event witness, not just
    an assertion about the return value."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    big_image = _image_block(6_000_000)
    client = _FakeMCPClient(content=[big_image])
    ctx = _make_gated_ctx(
        tmp_path, client,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )

    seen: list = []
    ctx.events.add_subscriber(seen.append)

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshot", args={})
    asyncio.run(_execute(op, ctx))

    denied = [e for e in seen if e.type == "mcp_media_denied"]
    assert denied, "expected an mcp_media_denied audit-event"
    assert denied[0].data.get("server") == "testsrv"
    assert denied[0].data.get("tool") == "screenshot"
    assert denied[0].data.get("size_bytes") == 6_000_000


def test_denial_is_visible_in_the_tool_result_text_not_just_the_event(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #4946 acceptance ④ (lead-coder, explicit) — the audit-event
    alone is not enough. The dropped image must be named in the tool
    result's own `content` text, the field the MODEL actually reads —
    `media_blocks` is filtered to `type == "image"` by its own consumer
    (router_loop.py's `_build_media_followup_message`), so a text note
    placed THERE would be silently discarded, leaving the model with "one
    fewer image, no sign anything was lost" — the exact silent-loss shape
    this session's #4961/#4954 arcs closed elsewhere tonight."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    big_image = _image_block(6_000_000)
    client = _FakeMCPClient(content=[big_image])
    ctx = _make_gated_ctx(
        tmp_path, client,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshot", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["content"], "expected a denial note in the tool result's text content"
    assert "not loaded" in result["content"] or "denied" in result["content"].lower(), (
        f"the tool result text must name the drop, not just describe it "
        f"implicitly; got {result['content']!r}"
    )


def test_under_limit_mcp_image_still_persists_normally(tmp_path, monkeypatch) -> None:
    """Tier 2: regression control — a correctly-sized MCP image still
    reaches media_blocks via media_store when a real gate is wired in
    (not just when permission_resolver=None bypasses it, as the sibling
    file's existing tests use)."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    small_image = _image_block(1_000)
    client = _FakeMCPClient(content=[small_image])
    ctx = _make_gated_ctx(
        tmp_path, client,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
    )

    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshot", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["status"] == "ok"
    surviving_data = {b.get("data") for b in result["media_blocks"] if isinstance(b, dict)}
    assert small_image["data"] in surviving_data, (
        f"the correctly-sized image must still reach media_blocks; got "
        f"{result['media_blocks']!r}"
    )
