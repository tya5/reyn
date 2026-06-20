"""Tier 2: web_fetch download-size cap prevents unbounded-memory DoS (#1913).

`client.get` materialized the ENTIRE response body into memory before
`max_length` (an extracted-TEXT cap) ever applied — so a hostile URL (or a
benign one that redirects to a huge payload) could exhaust memory. web_fetch now
streams with a byte ceiling (`web.fetch.max_download_bytes`), rejecting a
response whose `Content-Length` exceeds the cap (early, before download) or
whose streamed body runs past it (chunked / no Content-Length).

Falsification:
- body-over-cap: a body past the cap → status="too_large" (the cap is the gate —
  a larger cap lets the SAME body through → ok).
- content-length precheck: a declared CL over the cap is rejected WITHOUT
  consuming the body (the byte stream raises if touched, and we still get
  too_large — proving the early reject).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from reyn.config.media import WebConfig, WebFetchConfig
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp


def _make_ctx(max_download_bytes: int) -> Any:
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        def emit(self, *a: object, **k: object) -> None:
            pass

    return OpContext(
        workspace=type("W", (), {})(),  # type: ignore[arg-type]
        events=_FakeEventLog(),          # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_config=WebConfig(fetch=WebFetchConfig(max_download_bytes=max_download_bytes)),
    )


class _StreamResp:
    def __init__(self, *, body: bytes, content_length: str | None,
                 raise_on_iter: bool = False) -> None:
        hdrs = {"content-type": "text/plain"}
        if content_length is not None:
            hdrs["content-length"] = content_length
        self.headers = httpx.Headers(hdrs)
        self.status_code = 200
        self.charset_encoding = None
        self._body = body
        self._raise = raise_on_iter

    async def aiter_bytes(self):  # noqa: ANN201
        if self._raise:
            raise AssertionError("body must not be downloaded when CL exceeds cap")
        # deliver in small chunks so the byte-ceiling is exercised mid-stream
        for i in range(0, len(self._body), 16):
            yield self._body[i : i + 16]


class _StreamCtx:
    def __init__(self, resp: _StreamResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _StreamResp:
        return self._resp

    async def __aexit__(self, *a: object) -> None:
        return None


def _client_factory(resp: _StreamResp):
    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _StreamCtx:
            return _StreamCtx(resp)

    return _Client


def _run(monkeypatch, resp: _StreamResp, cap: int) -> dict:
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(resp))
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    return asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(cap), caller="control_ir"))


def test_body_over_cap_rejected(monkeypatch) -> None:
    """Tier 2: a streamed body past the cap → status='too_large'."""
    resp = _StreamResp(body=b"A" * 500, content_length=None)
    result = _run(monkeypatch, resp, cap=100)
    assert result["status"] == "too_large", result


def test_larger_cap_allows_same_body(monkeypatch) -> None:
    """Tier 2: the SAME body passes under a larger cap → ok (falsification).

    Proves the cap is the gate, not some unrelated rejection.
    """
    resp = _StreamResp(body=b"A" * 500, content_length=None)
    result = _run(monkeypatch, resp, cap=10_000)
    assert result["status"] == "ok", result
    assert result["content"] == "A" * 500


def test_content_length_over_cap_rejected_without_download(monkeypatch) -> None:
    """Tier 2: a declared Content-Length over the cap is rejected early.

    ``aiter_bytes`` raises if touched — getting ``too_large`` without that
    AssertionError proves the body was never downloaded (the precheck fired).
    """
    resp = _StreamResp(body=b"A" * 500, content_length="500", raise_on_iter=True)
    result = _run(monkeypatch, resp, cap=100)
    assert result["status"] == "too_large", result


def test_normal_body_ok(monkeypatch) -> None:
    """Tier 2: a body under the cap fetches normally (no regression)."""
    resp = _StreamResp(body=b"hello world", content_length="11")
    result = _run(monkeypatch, resp, cap=10 * 1024 * 1024)
    assert result["status"] == "ok", result
    assert result["content"] == "hello world"
