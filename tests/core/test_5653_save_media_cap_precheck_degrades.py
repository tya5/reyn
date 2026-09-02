"""Tier 2: #5653 — ``save_media`` now self-triggers the project-wide
storage cap pre-check (``MediaStore._evict_cross_session_over_cap``,
#5366 §3 / #4478), and each of the 4 real callers
(``web.py``/``file.py``/``mcp.py``/``router_loop.py``) degrades a
``MediaStoreWriteUnavailable`` from it individually, read per site (lead-
coder's explicit "no probably-fine" instruction) rather than assumed
uniform:

  - ``web.py``/``file.py``: neither had ANY local except around
    ``save_media`` — an unhandled raise would propagate to
    ``dispatch_tool``'s own generic ``except Exception``
    (``core/dispatch/dispatcher.py``), turning the WHOLE call into a
    typed error result and losing content the call already produced
    (fetched page text / read confirmation), not just the image. Both
    already have an inline-base64 fallback for "no MediaStore
    configured" — the fix reuses it for "MediaStore configured but
    refusing this write" too.
  - ``mcp.py``: loops over potentially several images in ONE result: an
    unhandled raise would abort every OTHER already-processed image in
    the same batch. Degrades exactly like its own neighbouring
    ``ctx.media_store is None`` branch (append the raw inline item,
    keep going) — not the ``denial_notes``/text-note shape used for a
    ``PermissionError`` just above it, which drops the image; this one
    keeps it.
  - ``router_loop.py``'s ``_as_path_ref``: already returns ``None`` on
    "no path obtainable" (undecodable base64) with an established
    contract that both its callers already handle gracefully — the fix
    is the same return-``None`` shape for this new failure too.

Real ``MediaStore`` instances throughout (the pinned-over-cap trigger
``tests/data/test_4478_media_eviction.py`` already established for
``save_tool_result`` — this file's own witness that ``save_media``
genuinely raises the SAME way now), real op handlers
(``handle_web_fetch``/``file.py``'s ``handle``/``mcp.py``'s
``_execute``), no mocks.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.config import MultimodalConfig
from reyn.config.infra import StorageConfig
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    MediaStoreWriteUnavailable,
)
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import collect_events


def _bump_mtime_forward(directory: Path) -> None:
    """Same determinism helper test_4478_media_eviction.py's own driver
    test uses — forces every existing file further into the past so
    write-order ties never race real filesystem mtime-tick granularity."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


def _over_cap_store(tmp_path: Path) -> MediaStore:
    """A real MediaStore whose NEXT write is refused: a pinned agent
    ("alice") already occupies the whole cap, so an unpinned agent's
    ("bob", the store this returns) own write finds no evictable
    candidate that would bring the project back under cap — the SAME
    real trigger test_4478_media_eviction.py's own
    test_raises_write_unavailable_when_media_and_history_content_both_
    pinned_over_cap uses, just for save_media instead of
    save_tool_result."""
    storage = StorageConfig(max_bytes=100, pin=["alice"])
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice.save_media(b"a" * 500, mime_type="image/png")
    _bump_mtime_forward(tmp_path)
    return MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )


# ---------------------------------------------------------------------------
# Core fix: save_media itself now runs the pre-check
# ---------------------------------------------------------------------------


def test_save_media_itself_raises_when_over_cap(tmp_path):
    """Tier 2: the root fix — before #5653, save_media never called
    _evict_cross_session_over_cap at all, so a media-only-heavy project
    never self-triggered eviction from its own writes. This is that same
    real over-cap scenario #4478's own witness uses for save_tool_result,
    now against save_media directly."""
    bob = _over_cap_store(tmp_path)
    with pytest.raises(MediaStoreWriteUnavailable):
        bob.save_media(b"b" * 500, mime_type="image/png")


# ---------------------------------------------------------------------------
# file.py — inline-base64 fallback (mirrors its own "no store" branch)
# ---------------------------------------------------------------------------


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)


def test_file_read_degrades_to_inline_base64_when_media_store_refuses(tmp_path, monkeypatch):
    """Tier 2: accept — read_file's own binary-image path does not crash
    (no exception propagates) when the real MediaStore refuses the
    write; it falls back to the SAME inline-base64 shape already used
    for "no MediaStore configured", and emits a named audit event."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.file import handle
    from reyn.schemas.models import FileIROp

    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
    (tmp_path / "shot.png").write_bytes(raw)

    events = EventLog()
    collected = collect_events(events)
    resolver = _resolver(tmp_path)
    workspace = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=workspace, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test",
        intervention_bus=_FakeBus(),  # type: ignore[arg-type]
        multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        media_store=_over_cap_store(tmp_path / "store"),
    )
    op = FileIROp(kind="file", op="read", path="shot.png")
    result = asyncio.run(handle(op, ctx))

    assert result["status"] == "ok", "a refused media write must never crash the read"
    (block,) = result["media_blocks"]
    assert block["type"] == "image"
    assert "data" in block and "path" not in block, (
        f"expected the inline-base64 degrade shape, got {block!r}"
    )
    assert base64.b64decode(block["data"]) == raw
    assert any(e.type == "file_read_media_write_unavailable" for e in collected), (
        "expected a named audit event for the degrade, not a silent one"
    )


# ---------------------------------------------------------------------------
# web.py — inline-base64 fallback (mirrors its own "no store" branch)
# ---------------------------------------------------------------------------


class _CapturingImageClient:
    body_bytes: bytes = b""
    content_type: str = "image/png"

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": type(self).content_type},
            content=type(self).body_bytes,
            request=httpx.Request("GET", "https://example.com/foo.png"),
        )

    async def __aenter__(self) -> "_CapturingImageClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response

    def stream(self, method: str, url: str) -> "_ResponseStreamCtx":
        return _ResponseStreamCtx(self._response)


class _ResponseStreamCtx:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeBus:
    async def request(self, iv):  # noqa: ANN001 — mirrors sibling test files
        from reyn.user_intervention import InterventionAnswer
        return InterventionAnswer(text="", choice_id="yes")


def test_web_fetch_degrades_to_inline_base64_when_media_store_refuses(tmp_path, monkeypatch):
    """Tier 2: accept — web_fetch's own binary-image path does not crash
    when the real MediaStore refuses the write; same inline-base64
    fallback + named audit event as file.py's own sibling test above."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    _CapturingImageClient.body_bytes = body
    _CapturingImageClient.content_type = "image/png"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingImageClient)

    events = EventLog()
    collected = collect_events(events)
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"}, project_root=tmp_path, interactive=True,
    )
    workspace = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=workspace, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        intervention_bus=_FakeBus(),  # type: ignore[arg-type]
        multimodal_config=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        media_store=_over_cap_store(tmp_path / "store"),
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com/foo.png")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx))

    assert result["status"] == "ok"
    (block,) = result["media_blocks"]
    assert block["type"] == "image"
    assert "data" in block and "path" not in block
    assert base64.b64decode(block["data"]) == body
    assert any(e.type == "web_fetch_media_write_unavailable" for e in collected)


# ---------------------------------------------------------------------------
# mcp.py — append-raw-item-and-continue (mirrors its own "no store" branch)
# ---------------------------------------------------------------------------


class _FakeMCPClient:
    def __init__(self, content: list[dict]) -> None:
        self._content = content

    async def call_tool(self, name, args, *, progress_callback=None, timeout_seconds=None):
        return {"content": self._content, "isError": False, "structuredContent": None}


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


def _image_block(size_bytes: int) -> dict:
    return {
        "type": "image",
        "data": base64.b64encode(b"x" * size_bytes).decode("ascii"),
        "mimeType": "image/png",
    }


def test_mcp_batch_keeps_going_when_media_store_refuses_mid_batch(tmp_path, monkeypatch):
    """Tier 2: accept — a real MediaStore refusal on ONE image inside a
    multi-image mcp result must not abort the whole batch (the failure
    mode an unhandled raise inside this loop would have produced — every
    OTHER already-processed image lost too). Both images degrade to the
    inline shape (the store never frees the cap between them), the
    surrounding text survives, and an audit event fires per degraded
    item."""
    monkeypatch.chdir(tmp_path)
    from reyn.core.op_runtime.mcp import _execute
    from reyn.schemas.models import MCPIROp

    small_1 = _image_block(1_000)
    small_2 = _image_block(2_000)
    text_block = {"type": "text", "text": "here are the screenshots"}
    client = _FakeMCPClient(content=[text_block, small_1, small_2])

    events = EventLog()
    collected = collect_events(events)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)
    ctx = OpContext(
        workspace=Workspace(events=events, permission_resolver=resolver),
        events=events, permission_decl=PermissionDecl(), permission_resolver=resolver,
        mcp_servers={"testsrv": {"type": "stdio", "command": "fake"}},
        mcp_pool=_StubPool(client),
        media_store=_over_cap_store(tmp_path / "store"),
    )
    op = MCPIROp(kind="mcp", server="testsrv", tool="screenshots", args={})
    result = asyncio.run(_execute(op, ctx))

    assert result["status"] == "ok", "a refused write on one image must not fail the whole batch"
    surviving_data = {b.get("data") for b in result["media_blocks"] if isinstance(b, dict)}
    assert small_1["data"] in surviving_data, "the first image must survive the degrade, not vanish"
    assert small_2["data"] in surviving_data, "the second image must survive too — batch not aborted"
    assert "here are the screenshots" in result["content"]
    write_unavailable_events = [e for e in collected if e.type == "mcp_media_write_unavailable"]
    degraded_sizes = {e.data.get("size_bytes") for e in write_unavailable_events}
    assert degraded_sizes == {1_000, 2_000}, (
        "expected BOTH images to have individually triggered their own "
        f"write-unavailable event (identified by their real byte size), "
        f"not just one overall; got sizes {degraded_sizes!r}"
    )


# ---------------------------------------------------------------------------
# router_loop.py — _as_path_ref returns None (mirrors "undecodable base64")
# ---------------------------------------------------------------------------


def test_as_path_ref_returns_none_when_media_store_refuses(tmp_path):
    """Tier 2: accept — _as_path_ref's own established "no path obtainable
    -> None, caller degrades consciously" contract now also covers a real
    MediaStoreWriteUnavailable, not just undecodable base64. Both of its
    real callers (router_loop.py's own budget/tail-preview code) already
    treat None as "skip this item" — verified by that code's own existing
    tests; this pins the NEW branch that reaches None specifically."""
    from reyn.runtime.router_loop import _as_path_ref

    block = {
        "type": "image",
        "data": base64.b64encode(b"x" * 500).decode("ascii"),
        "mime_type": "image/png",
    }
    ref = _as_path_ref(
        block, _over_cap_store(tmp_path), tool_name="t", seq=1,
    )
    assert ref is None
