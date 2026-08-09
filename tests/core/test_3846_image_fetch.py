"""Tier 2: `core/present/image_fetch.py` — #3846 ① image-src resolution.

``pin_ssrf=True`` is unconditional in this module (never a caller choice).
Its own loopback denial is UNconditional too (`_ssrf_guard._deny_reason`
checks `ip.is_loopback` before the `allow_private` opt-in even applies), so —
same as `tests/test_web_fetch_download_cap_1913.py`'s own approach for the
identical reason — a real local `HTTPServer` on 127.0.0.1 is structurally
unreachable through the pinned transport and cannot serve as "a real
collaborator" here; there is no real, deterministic remote HTTPS image host
to fetch in CI either. The httpx.AsyncClient stand-in below is a real-shaped
class (implements the exact `__aenter__`/`__aexit__`/`.stream()` surface this
module calls), not a MagicMock — same idiom the existing web_fetch download-
cap test already uses for this exact class of problem.

The SSRF-block path itself is `_ssrf_guard`'s own tested contract
(`tests/test_ssrf_guard_1956.py`) — not re-proven here. What IS tested here
is `image_fetch`'s OWN logic: scheme gate, allowlist gate, size cap, SSRFBlocked
wrapping, and that a normal response round-trips (bytes + content-type).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from reyn.core.present.image_fetch import ImageFetchError, fetch_image_bytes


class _StreamResp:
    def __init__(
        self, *, body: bytes, content_type: str = "image/png", status: int = 200,
    ) -> None:
        self.headers = httpx.Headers({"content-type": content_type})
        self.status_code = status
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://example.invalid/x")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=request, response=response
            )

    async def aiter_bytes(self):  # noqa: ANN201
        for i in range(0, len(self._body), 16):
            yield self._body[i : i + 16]


class _StreamCtx:
    def __init__(self, resp: "_StreamResp | Exception") -> None:
        self._resp = resp

    async def __aenter__(self) -> _StreamResp:
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    async def __aexit__(self, *a: object) -> None:
        return None


def _client_factory(resp: "_StreamResp | Exception"):
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


def _fetch(monkeypatch, resp: "_StreamResp | Exception", **kwargs: Any):
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(resp))
    return asyncio.run(fetch_image_bytes("https://example.com/x.png", **kwargs))


def test_fetches_bytes_and_content_type(monkeypatch) -> None:
    """Tier 2: a normal 200 response returns (body, content-type) verbatim."""
    body, content_type = _fetch(
        monkeypatch, _StreamResp(body=b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
    )
    assert body == b"\x89PNG\r\n\x1a\nfake"
    assert content_type == "image/png"


def test_non_http_scheme_rejected_without_a_client_call(monkeypatch) -> None:
    """Tier 2: a non-http(s) scheme (file://) is rejected by the scheme gate
    itself — httpx.AsyncClient is monkeypatched to RAISE if constructed, so
    this test also proves the gate fires before any client is even built."""
    def _boom(**kwargs):
        raise AssertionError("no client should be constructed for a bad scheme")
    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    with pytest.raises(ImageFetchError, match="not http/https"):
        asyncio.run(fetch_image_bytes("file:///etc/passwd"))


def test_allowed_schemes_narrows_within_http_https(monkeypatch) -> None:
    """Tier 2: `allowed_schemes=["https"]` rejects a plain-http src even
    though http is itself a fetchable scheme in general — the opt-in
    allowlist narrows WITHIN the fetchable set, without a client call."""
    def _boom(**kwargs):
        raise AssertionError("the allowlist gate must fire before any fetch")
    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    with pytest.raises(ImageFetchError, match="not in the configured"):
        asyncio.run(
            fetch_image_bytes("http://example.com/x.png", allowed_schemes=["https"])
        )


def test_allowed_schemes_permits_a_listed_scheme(monkeypatch) -> None:
    """Tier 2: falsification pair — the SAME allowlist mechanism does not
    block a scheme that IS listed (proves the gate discriminates, not just
    denies everything)."""
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(_StreamResp(body=b"ok"))
    )
    body, _ = asyncio.run(
        fetch_image_bytes("http://example.com/x.png", allowed_schemes=["http"])
    )
    assert body == b"ok"


def test_response_over_the_byte_cap_is_rejected(monkeypatch) -> None:
    """Tier 2: a response larger than `max_bytes` raises rather than being
    silently truncated or fully buffered — a streamed check, since this
    stand-in sends no Content-Length header at all."""
    with pytest.raises(ImageFetchError, match="exceeds the"):
        _fetch(monkeypatch, _StreamResp(body=b"A" * 500), max_bytes=100)


def test_a_larger_cap_allows_the_same_response(monkeypatch) -> None:
    """Tier 2: falsification pair — raising the cap past the same response's
    size lets it through, proving the cap (not something else) was the
    reject reason above."""
    body, _ = _fetch(monkeypatch, _StreamResp(body=b"A" * 500), max_bytes=10_000)
    assert body == b"A" * 500


def test_http_error_status_is_reported(monkeypatch) -> None:
    """Tier 2: a 404 response raises ImageFetchError naming the status,
    rather than returning the error page's body as if it were image bytes."""
    with pytest.raises(ImageFetchError, match="404"):
        _fetch(monkeypatch, _StreamResp(body=b"", status=404))


def test_timeout_is_reported(monkeypatch) -> None:
    """Tier 2: an httpx timeout is wrapped as ImageFetchError, naming the
    configured timeout — not left as a bare httpx exception the presenter
    layer would need to know about specifically."""
    with pytest.raises(ImageFetchError, match="timed out"):
        _fetch(monkeypatch, httpx.TimeoutException("timed out"), timeout=2.5)


def test_ssrf_denial_is_reported_as_image_fetch_error(monkeypatch) -> None:
    """Tier 2: the SSRF pin's own denial (SSRFBlocked, a PermissionError
    subclass — NOT an httpx.RequestError) is wrapped too, so the presenter
    layer catches exactly ONE exception type regardless of WHY the fetch
    failed. `_ssrf_guard`'s own suite proves loopback/link-local/etc are
    actually denied; this proves image_fetch does not let that denial leak
    past its own error boundary as a raw, uncaught exception type."""
    from reyn._ssrf_guard import SSRFBlocked

    with pytest.raises(ImageFetchError):
        _fetch(monkeypatch, SSRFBlocked("blocked fetch to 127.0.0.1 (loopback)"))
