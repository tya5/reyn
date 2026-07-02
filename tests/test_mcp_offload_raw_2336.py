"""Tier 2: #2336 follow-up — a large MCP op result offloads CLEAN, not a whole-dict single-line.

The op result used to carry ``raw`` (the full flattened CallToolResult), which re-carried the same
oversized ``content`` text. So a large result had TWO oversized fields (``content`` + ``raw``) →
``_oversized_fields != [_offload_payload_field]`` → the clean-payload gate missed → the whole dict
was stored as one indent-less JSON line (owner's offload file was a nested single-line envelope).
webfetch was unaffected only because ``content`` was its sole large field.

Fix (op-side ``mcp.py`` only, P7-safe — OS/context_builder/store untouched): drop ``raw`` (``isError``
is already ``status``, the joined text is already ``content``), and preserve the only non-duplicate
SDK field — ``structuredContent`` — as ``structured`` ONLY when present. Now ``content`` is the sole
oversized field → the clean-payload gate fires → the offload file holds clean text with real
newlines. Real ``_execute`` via a stubbed MCP client (no subprocess); real ``offload_control_ir_result``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES as CAP
from reyn.core.context_builder import _oversized_fields, offload_control_ir_result
from reyn.services.offload.store import read_offloaded

# A multi-line text payload well over the per-field offload threshold — mirrors a big MCP tool dump.
_BIG_TEXT = "\n".join(f"row {i}: " + "d" * 80 for i in range((CAP // 40) + 200))


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
        mcp_clients={"testsrv": mcp_client},  # type: ignore[dict-item]
    )


def _run(content: list[dict], tmp_path: Path, **kw) -> dict:
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    ctx = _make_ctx(tmp_path, _FakeMCPClient(content, **kw))
    op = MCPIROp(kind="mcp", server="testsrv", tool="dump", args={})
    return asyncio.run(_execute(op, ctx))


def test_no_raw_field_content_is_sole_oversized(tmp_path, monkeypatch):
    """Tier 2: CORE — a large MCP result has NO ``raw`` field, so ``content`` is the sole oversized
    field. RED on main: ``raw`` re-carried ``content`` → ``_oversized_fields == ["content", "raw"]``."""
    monkeypatch.chdir(tmp_path)
    result = _run([{"type": "text", "text": _BIG_TEXT}], tmp_path)

    assert "raw" not in result, "the content-duplicating `raw` field is dropped"
    assert _oversized_fields(result) == ["content"], \
        "content is the SOLE oversized field (clean-payload gate can fire)"


def test_large_mcp_result_offloads_clean_content(tmp_path, monkeypatch):
    """Tier 2: the bug — a large MCP result offloads as CLEAN text (real newlines), not a whole-dict
    single-line JSON envelope. RED on main (whole-dict fallback because content+raw both oversized)."""
    monkeypatch.chdir(tmp_path)
    result = _run([{"type": "text", "text": _BIG_TEXT}], tmp_path)

    inline = offload_control_ir_result(result, 0, tmp_path)
    stored = Path(inline["_offload_ref"]).read_text(encoding="utf-8")

    assert not stored.lstrip().startswith("{"), "offload file is clean content, not a JSON dict envelope"
    assert stored == _BIG_TEXT, "the file is exactly the clean joined text"
    assert "\\n" not in stored, "real newlines (not JSON-escaped) — no JSON-of-JSON symptom"
    assert stored.count("\n") == _BIG_TEXT.count("\n"), "all newlines preserved"
    text, _ = read_offloaded(inline["_offload_ref"], base_dir=tmp_path)
    assert text == _BIG_TEXT, "read-back via the store returns the clean payload"


def test_structured_content_preserved_when_present(tmp_path, monkeypatch):
    """Tier 2: a real MCP structured output is preserved as ``structured`` (no in-context data loss),
    and it does not re-carry ``content`` — so ``content`` stays the sole oversized field."""
    monkeypatch.chdir(tmp_path)
    structured = {"rows": [1, 2, 3], "schema": "v1"}
    result = _run([{"type": "text", "text": _BIG_TEXT}], tmp_path, structured=structured)

    assert result["structured"] == structured, "structuredContent preserved as `structured`"
    assert _oversized_fields(result) == ["content"], "structured (small) does not add a second oversized field"


def test_structured_absent_when_none(tmp_path, monkeypatch):
    """Tier 2: when the tool returns no structured output (the default), there is NO ``structured``
    field — clean end-state, no shim key."""
    monkeypatch.chdir(tmp_path)
    result = _run([{"type": "text", "text": "small"}], tmp_path)  # structured defaults to None

    assert "structured" not in result, "no `structured` field when structuredContent is None"
    assert "raw" not in result


def test_small_mcp_result_not_offloaded_no_regression(tmp_path, monkeypatch):
    """Tier 2: a small MCP result is not oversized → not offloaded (inline unchanged). No regression
    for the common case."""
    monkeypatch.chdir(tmp_path)
    result = _run([{"type": "text", "text": "hello world"}], tmp_path)

    assert _oversized_fields(result) == [], "small content is not oversized"
    inline = offload_control_ir_result(result, 0, tmp_path)
    assert "_offload_ref" not in inline, "small result is not offloaded"
    assert inline["content"] == "hello world", "content stays inline"
