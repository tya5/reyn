"""Tier 2: web_fetch start_index pagination (issue #357).

WebFetchIROp.start_index lets the LLM page through content past
max_length: call once with start_index=0, then call again with
start_index=next_start until next_start is None.

These tests pin:
  - start_index=0 + content > max_length → truncated=True, next_start=max_length
  - start_index=N + remaining ≤ max_length → truncated=False, next_start=None
  - start_index past end-of-content → empty content, truncated=False
  - Default behaviour (= no start_index) is byte-identical to pre-#357

Real OpContext + CapturingClient. No collaborator mocks.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from reyn.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp


def _make_ctx() -> Any:
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        subscribers: list = []
        def emit(self, *args: object, **kwargs: object) -> None:
            pass

    class _FakeWorkspace:
        pass

    return OpContext(
        workspace=_FakeWorkspace(),  # type: ignore[arg-type]
        events=_FakeEventLog(),      # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_config=None,
    )


class _CapturingTextClient:
    """Returns a fixed text/plain body so extraction is bypassed and the
    pagination logic operates on a predictable string."""

    body: str = ""

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=type(self).body.encode(),
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CapturingTextClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response


# ── default behaviour preserved (no start_index supplied) ──────────────


def test_default_no_start_index_returns_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: when start_index is unset (= default 0), behaviour matches
    pre-#357: returns content[:max_length], truncated reflects whether
    more exists. New `next_start` is non-null when truncated.
    """
    _CapturingTextClient.body = "A" * 200
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50)
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(), caller="control_ir"))

    assert result["content"] == "A" * 50
    assert result["truncated"] is True
    assert result["start_index"] == 0
    assert result["next_start"] == 50
    assert result["total_length"] == 200


# ── start_index continuation ───────────────────────────────────────────


def test_start_index_returns_suffix_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: start_index=50 + max_length=50 + total=200 → returns
    bytes [50:100], truncated=True (more remaining), next_start=100.
    """
    _CapturingTextClient.body = "A" * 50 + "B" * 50 + "C" * 50 + "D" * 50
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com",
        max_length=50, start_index=50,
    )
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(), caller="control_ir"))

    assert result["content"] == "B" * 50
    assert result["truncated"] is True
    assert result["start_index"] == 50
    assert result["next_start"] == 100
    assert result["total_length"] == 200


def test_start_index_final_chunk_clears_next_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: when start_index + max_length ≥ total_length, the returned
    chunk is the last one: truncated=False, next_start=None.
    """
    _CapturingTextClient.body = "A" * 50 + "B" * 50
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com",
        max_length=100, start_index=50,
    )
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(), caller="control_ir"))

    assert result["content"] == "B" * 50
    assert result["truncated"] is False
    assert result["start_index"] == 50
    assert result["next_start"] is None
    assert result["total_length"] == 100


def test_start_index_past_end_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: start_index past end-of-content is safe: returns empty
    content, truncated=False, next_start=None. Avoids IndexError /
    negative-slice surprises.
    """
    _CapturingTextClient.body = "hello"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com",
        max_length=50, start_index=1000,
    )
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(), caller="control_ir"))

    assert result["content"] == ""
    assert result["truncated"] is False
    assert result["next_start"] is None
    assert result["total_length"] == 5


# ── full-document fits in one fetch ────────────────────────────────────


def test_small_content_returns_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: content ≤ max_length, start_index=0 → entire content
    returned in one call, truncated=False, next_start=None.
    """
    _CapturingTextClient.body = "short content"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(), caller="control_ir"))

    assert result["content"] == "short content"
    assert result["truncated"] is False
    assert result["next_start"] is None
    assert result["total_length"] == len("short content")


# ── round-trip: two calls covering the whole document ──────────────────


def test_two_call_pagination_covers_whole_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: simulate the LLM-driven pagination pattern — call once
    with start_index=0, then with start_index=next_start. The two
    chunks concatenated must equal the full content.
    """
    _CapturingTextClient.body = "".join(f"line{i:04d}\n" for i in range(20))  # 180 chars
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    op1 = WebFetchIROp(
        kind="web_fetch", url="https://example.com",
        max_length=100, start_index=0,
    )
    r1 = asyncio.run(handle_web_fetch(op=op1, ctx=_make_ctx(), caller="control_ir"))
    assert r1["truncated"] is True
    assert r1["next_start"] is not None

    op2 = WebFetchIROp(
        kind="web_fetch", url="https://example.com",
        max_length=100, start_index=r1["next_start"],
    )
    r2 = asyncio.run(handle_web_fetch(op=op2, ctx=_make_ctx(), caller="control_ir"))
    assert r2["truncated"] is False
    assert r2["next_start"] is None

    assert r1["content"] + r2["content"] == _CapturingTextClient.body
