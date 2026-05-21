"""Tier 2: web_fetch returns preview + path-ref when MediaStore is wired
(#385 PoC PR-D).

Pins the new return shape introduced by #385 PoC: instead of inlining the
full extracted body in ``result["content"]``, the tool writes the body to
``.reyn/tool-results/`` via :meth:`MediaStore.save_tool_result` and
returns a structured ``preview`` plus a ``path_ref`` block. The LLM then
calls ``read_tool_result(path=...)`` to load the body on demand
(= lazy expand).

Contract pins:

1. HTML content → preview dict carries ``title`` / ``outline`` /
   ``first_paragraph`` / ``link_count`` extracted from the raw HTML.
   Same input HTML always produces the same preview (= pure-function
   determinism required to keep sandbox_2 N-runs reproducible per the
   cofounder warning on preview drift).
2. ``content`` field is empty when MediaStore stores the body; the
   path-ref carries the location.
3. ``path_ref`` block matches MediaStore's ``save_tool_result`` shape
   (= ``{type, path, mime_type, content_hash}``) and the file actually
   exists on disk.
4. Backward compatibility: when ``ctx.media_store is None``, the
   pre-#385 return shape (= ``content`` inlined) is unchanged so legacy
   sessions / phase-side callers don't break.
5. Plain text fallback: non-HTML text content yields a ``preview`` with
   ``first_lines`` + ``line_count`` + ``content_chars`` so the LLM
   still gets a structured summary.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp
from reyn.workspace.media_store import MediaStore, MediaStoreConfig


def _ctx_with_media_store(tmp_path: Path) -> Any:
    """Build a real OpContext with a MediaStore rooted at ``tmp_path``."""
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        subscribers: list = []
        events: list = []
        def emit(self, kind: str, **kwargs: Any) -> None:
            self.events.append((kind, kwargs))

    class _FakeWorkspace:
        pass

    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    return OpContext(
        workspace=_FakeWorkspace(),                   # type: ignore[arg-type]
        events=_FakeEventLog(),                        # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_config=None,
        media_store=store,
    )


def _ctx_without_media_store() -> Any:
    """Pre-#385 OpContext (= no MediaStore wired)."""
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        subscribers: list = []
        def emit(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _FakeWorkspace:
        pass

    return OpContext(
        workspace=_FakeWorkspace(),                   # type: ignore[arg-type]
        events=_FakeEventLog(),                        # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_config=None,
    )


class _CapturingHtmlClient:
    """httpx.AsyncClient stand-in that returns a fixed HTML response."""

    body: str = ""
    content_type: str = "text/html"

    def __init__(self, **kwargs: Any) -> None:
        self._response = httpx.Response(
            200,
            headers={"content-type": type(self).content_type},
            content=type(self).body.encode("utf-8"),
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CapturingHtmlClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response


_SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Sample Article Title</title></head>
<body>
<h1>Top Heading</h1>
<p>The opening paragraph introduces the topic and outlines what the rest of the page will cover.</p>
<h2>Section One</h2>
<p>Details about section one go here.</p>
<a href="https://a.example/">link a</a>
<a href="https://b.example/">link b</a>
<h2>Section Two</h2>
<p>Details about section two.</p>
<a href="https://c.example/">link c</a>
</body></html>
"""


# ── HTML preview shape ─────────────────────────────────────────────────


def test_html_fetch_returns_preview_and_path_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: HTML response → preview dict with title / outline /
    first_paragraph / link_count + path_ref block; content emptied.
    """
    _CapturingHtmlClient.body = _SAMPLE_HTML
    _CapturingHtmlClient.content_type = "text/html"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHtmlClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com", max_length=50_000,
    )
    ctx = _ctx_with_media_store(tmp_path)
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["stored_as"] == "path_ref"
    assert result["content"] == ""

    preview = result["preview"]
    assert preview["title"] == "Sample Article Title"
    # H1 + 2x H2 → outline length 3, prefixed by tag name
    assert any(line.startswith("H1: Top Heading") for line in preview["outline"])
    assert any(line.startswith("H2: Section One") for line in preview["outline"])
    assert any(line.startswith("H2: Section Two") for line in preview["outline"])
    assert "opening paragraph" in preview["first_paragraph"]
    assert preview["link_count"] == 3
    assert preview["content_chars"] > 0

    path_ref = result["path_ref"]
    assert path_ref["type"] == "tool_result_ref"
    assert path_ref["mime_type"].startswith("text/html")
    assert path_ref["content_hash"].startswith("sha256:")

    # The file referenced exists and contains the extracted body.
    full = tmp_path / path_ref["path"]
    assert full.exists()
    body = full.read_text(encoding="utf-8")
    assert "opening paragraph" in body


def test_html_preview_is_deterministic_for_same_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: same input HTML → same preview output across runs.

    Pins the pure-function determinism contract that sandbox_2's
    measurement methodology relies on (= preview drift would noise the
    "読まずに answer 率" N-runs).
    """
    _CapturingHtmlClient.body = _SAMPLE_HTML
    _CapturingHtmlClient.content_type = "text/html"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHtmlClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com", max_length=50_000,
    )

    r1 = asyncio.run(
        handle_web_fetch(
            op=op, ctx=_ctx_with_media_store(tmp_path), caller="control_ir",
        ),
    )
    r2 = asyncio.run(
        handle_web_fetch(
            op=op, ctx=_ctx_with_media_store(tmp_path), caller="control_ir",
        ),
    )

    # Preview content is byte-identical regardless of timestamp /
    # filename variance — the FILE name changes per save (= timestamp
    # token), but the preview body and the content_hash do not.
    assert r1["preview"] == r2["preview"]
    assert r1["path_ref"]["content_hash"] == r2["path_ref"]["content_hash"]


# ── plain text fallback ────────────────────────────────────────────────


def test_plain_text_fetch_returns_first_lines_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: non-HTML text content yields a ``first_lines`` preview
    instead of the HTML-shaped dict.
    """
    _CapturingHtmlClient.body = "\n".join(f"line {i}" for i in range(20))
    _CapturingHtmlClient.content_type = "text/plain"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHtmlClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com/log.txt", max_length=50_000,
    )
    ctx = _ctx_with_media_store(tmp_path)
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["stored_as"] == "path_ref"
    preview = result["preview"]
    assert "first_lines" in preview
    assert preview["first_lines"][0] == "line 0"
    assert preview["line_count"] == 20
    assert preview["content_chars"] > 0


# ── backward compatibility ─────────────────────────────────────────────


def test_legacy_no_media_store_path_returns_inline_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when MediaStore is not wired, return shape is pre-#385
    (= ``content`` inlined, no ``preview`` / ``path_ref`` keys, no
    ``stored_as`` key).
    """
    _CapturingHtmlClient.body = _SAMPLE_HTML
    _CapturingHtmlClient.content_type = "text/html"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingHtmlClient)

    op = WebFetchIROp(
        kind="web_fetch", url="https://example.com", max_length=50_000,
    )
    result = asyncio.run(
        handle_web_fetch(
            op=op, ctx=_ctx_without_media_store(), caller="control_ir",
        ),
    )

    assert result["status"] == "ok"
    # content is the extracted body (= what every pre-PoC test asserts).
    assert result["content"]  # non-empty
    assert "Top Heading" in result["content"]
    assert "preview" not in result
    assert "path_ref" not in result
    assert "stored_as" not in result
