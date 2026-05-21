"""Tier 2: web_fetch HTML extractor selection (issue #355).

The web_fetch handler picks an HTML extractor at runtime:
  - `trafilatura` (= optional `reyn[fetch]` extra) when importable AND it
    returns a non-empty result.
  - stdlib `_TextExtractor` (= html.parser-based) otherwise.

These tests pin the selection logic + the result envelope shape. They use
real `_extract_html_text` invocations + a CapturingClient for the
end-to-end handler tests — no mocks of collaborators.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.op_runtime.web import _extract_html_text, handle_web_fetch
from reyn.schemas.models import WebFetchIROp

# ── helpers ────────────────────────────────────────────────────────────


def _make_ctx() -> Any:
    """Minimal OpContext for handler tests (no permission gate, no workspace ops)."""
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


class _CapturingHTMLClient:
    """httpx.AsyncClient drop-in that returns a fixed HTML body."""

    html_body: str = ""

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=type(self).html_body.encode(),
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CapturingHTMLClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response


class _CapturingTextClient:
    """httpx.AsyncClient drop-in that returns text/plain (= no extractor)."""

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"hello world",
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CapturingTextClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response


# ── _extract_html_text unit tests ──────────────────────────────────────


def test_extract_returns_trafilatura_when_available() -> None:
    """Tier 2: when trafilatura is importable and extracts content, the
    extractor name reported is "trafilatura".

    Skip the test if trafilatura is not installed in this environment —
    that path is covered by the fallback test below.
    """
    pytest.importorskip("trafilatura")
    html_body = (
        "<html><head><title>T</title></head>"
        "<body><nav>nav</nav>"
        "<article><p>This is the main article body. " * 10
        + "It is long enough that trafilatura considers it content.</p></article>"
        "<footer>footer</footer></body></html>"
    )
    text, extractor = _extract_html_text(html_body)
    assert extractor == "trafilatura"
    assert "main article body" in text
    # trafilatura should strip nav/footer boilerplate (= the quality win
    # over stdlib parser). Pin only the strongest invariant here.
    assert "nav" not in text or "footer" not in text


def test_extract_falls_back_to_stdlib_when_trafilatura_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when `import trafilatura` raises ImportError, the extractor
    falls back to the stdlib `_TextExtractor` and reports name "stdlib".

    Simulated by inserting None into sys.modules and clearing any cached
    real module so the `import trafilatura` line inside `_extract_html_text`
    raises ImportError on the next call.
    """
    monkeypatch.setitem(sys.modules, "trafilatura", None)

    html_body = "<html><body><p>hello</p></body></html>"
    text, extractor = _extract_html_text(html_body)
    assert extractor == "stdlib"
    assert "hello" in text


def test_extract_falls_back_to_stdlib_when_trafilatura_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when trafilatura imports but returns None (= no main content
    detected, common on tiny / malformed pages), fallback to stdlib.

    Reason: trafilatura is conservative and can refuse to extract from
    pages it considers "boilerplate-only". The stdlib parser is naive
    but always returns something — better than empty content.
    """
    class _FakeTrafilatura:
        @staticmethod
        def extract(html_content: str) -> str | None:
            return None

    monkeypatch.setitem(sys.modules, "trafilatura", _FakeTrafilatura)

    html_body = "<html><body><p>tiny page</p></body></html>"
    text, extractor = _extract_html_text(html_body)
    assert extractor == "stdlib"
    assert "tiny page" in text


# ── handle_web_fetch result envelope tests ─────────────────────────────


def test_result_envelope_contains_extractor_field_for_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: HTML response → result dict carries `extractor` field with
    either "trafilatura" or "stdlib" (= one of the two known extractors).
    """
    _CapturingHTMLClient.html_body = (
        "<html><body><p>" + "lorem ipsum dolor sit amet. " * 20 + "</p></body></html>"
    )
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHTMLClient)

    ctx = _make_ctx()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert "extractor" in result
    assert result["extractor"] in {"trafilatura", "stdlib"}


def test_result_envelope_extractor_none_for_non_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: non-HTML response (text/plain, application/json, etc.) →
    `extractor` field is the literal "none" (= no extraction was performed,
    raw body returned).
    """
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingTextClient)

    ctx = _make_ctx()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["extractor"] == "none"
    assert result["content"] == "hello world"


def test_result_envelope_uses_stdlib_when_trafilatura_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: end-to-end fallback — when trafilatura is unavailable at
    handler-call time, the result envelope reports `extractor="stdlib"`.
    """
    monkeypatch.setitem(sys.modules, "trafilatura", None)

    _CapturingHTMLClient.html_body = "<html><body><p>hello world</p></body></html>"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHTMLClient)

    ctx = _make_ctx()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["extractor"] == "stdlib"
    assert "hello world" in result["content"]
