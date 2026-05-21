"""web kind handlers — web_fetch and web_search."""
from __future__ import annotations

import asyncio
import html.parser
from typing import Literal

from reyn.schemas.models import WebFetchIROp, WebSearchIROp

from . import register
from .context import OpContext


class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def text(self) -> str:
        return "\n".join(self._parts)


def _extract_html_text(html_content: str) -> tuple[str, str]:
    """Extract readable text from HTML.

    Tries `trafilatura` (= production-grade extractor, optional `reyn[fetch]`
    extra) first; falls back to the stdlib `_TextExtractor` when trafilatura
    is unavailable OR returns no main content. Issue #355.

    Returns ``(text, extractor_name)`` where extractor_name is
    ``"trafilatura"`` or ``"stdlib"``.
    """
    try:
        import trafilatura
    except ImportError:
        trafilatura = None  # type: ignore[assignment]

    if trafilatura is not None:
        extracted = trafilatura.extract(html_content)
        if extracted:
            return extracted, "trafilatura"

    parser = _TextExtractor()
    parser.feed(html_content)
    return parser.text(), "stdlib"


class _HtmlPreviewParser(html.parser.HTMLParser):
    """Distill an HTML page to (title, outline, first_paragraph, link_count).

    Pure-function deterministic preview generator for #385 PoC. Designed so
    the same input HTML always produces the same preview output — required
    to keep sandbox_2 dogfood measurement N-runs reproducible (= the
    "preview deterministic 化" cofounder warning).

    No LLM involvement; pure structural extraction so the preview is a
    stable function of the input bytes.
    """

    _HEADING_TAGS = {"h1", "h2", "h3"}
    _OUTLINE_MAX = 8

    def __init__(self) -> None:
        super().__init__()
        self._title_parts: list[str] = []
        self._in_title = False
        self._outline: list[tuple[str, list[str]]] = []  # (tag, buf)
        self._current_heading: tuple[str, list[str]] | None = None
        self._first_paragraph_parts: list[str] = []
        self._in_first_paragraph = False
        self._first_paragraph_done = False
        self._link_count = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self._HEADING_TAGS and len(self._outline) < self._OUTLINE_MAX:
            self._current_heading = (tag, [])
        elif (
            tag == "p"
            and not self._first_paragraph_done
            and not self._in_first_paragraph
        ):
            self._in_first_paragraph = True
        elif tag == "a":
            self._link_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self._HEADING_TAGS and self._current_heading is not None:
            heading = self._current_heading
            self._outline.append(heading)
            self._current_heading = None
        elif tag == "p" and self._in_first_paragraph:
            self._in_first_paragraph = False
            if self._first_paragraph_parts:
                self._first_paragraph_done = True

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        if self._in_title:
            self._title_parts.append(stripped)
        if self._current_heading is not None:
            self._current_heading[1].append(stripped)
        if self._in_first_paragraph and not self._first_paragraph_done:
            self._first_paragraph_parts.append(stripped)

    def result(self) -> dict:
        title = " ".join(self._title_parts).strip()
        outline = [
            f"{tag.upper()}: {' '.join(parts).strip()}"[:120]
            for tag, parts in self._outline
            if parts
        ]
        first_paragraph = " ".join(self._first_paragraph_parts).strip()
        if len(first_paragraph) > 280:
            first_paragraph = first_paragraph[:277] + "…"
        return {
            "title": title,
            "outline": outline,
            "first_paragraph": first_paragraph,
            "link_count": self._link_count,
        }


def _generate_web_fetch_preview(
    raw_html: str,
    *,
    extracted_text: str,
    content_type: str,
) -> dict:
    """Build a structured preview dict for a web_fetch result (#385 PoC).

    Pure function — same inputs produce same output. HTML inputs use
    :class:`_HtmlPreviewParser` to extract title / outline / first paragraph
    / link count. Non-HTML text falls back to a small structured summary
    of the first lines so the LLM still has something to gauge "is the
    extracted body the answer I need".

    Returns a dict with keys appropriate to the content type:

      HTML  → ``{title, outline, first_paragraph, link_count, content_chars}``
      other → ``{first_lines, line_count, content_chars}``
    """
    content_chars = len(extracted_text)
    if "text/html" in content_type:
        try:
            parser = _HtmlPreviewParser()
            parser.feed(raw_html)
            html_preview = parser.result()
        except Exception:
            html_preview = {
                "title": "", "outline": [],
                "first_paragraph": "", "link_count": 0,
            }
        html_preview["content_chars"] = content_chars
        return html_preview
    # Plain text / JSON / unknown — surface the head of the extracted body.
    lines = extracted_text.splitlines()
    first_lines = lines[:10]
    return {
        "first_lines": first_lines,
        "line_count": len(lines),
        "content_chars": content_chars,
    }


def _resolve_ssl_verify(ctx: OpContext) -> bool | str:
    """Resolve the SSL verify value for httpx from config + env fallback.

    Priority (highest → lowest):
      1. ``web.fetch.ca_bundle`` set in config → returns the CA bundle path (str).
      2. ``web.fetch.verify_ssl`` set to False → returns False (disable SSL check).
      3. ``web.fetch.verify_ssl`` set to True  → returns True (force SSL check).
      4. Both unset (None) → falls through to litellm.get_ssl_verify()
         (= SSL_VERIFY env → litellm.ssl_verify → SSL_CERT_FILE → True).
    """
    from litellm.llms.custom_httpx.http_handler import get_ssl_verify

    cfg = ctx.web_config.fetch if ctx.web_config is not None else None
    if cfg is not None:
        if cfg.ca_bundle:
            return cfg.ca_bundle  # custom CA bundle path (corporate PKI)
        if cfg.verify_ssl is False:
            return False
        if cfg.verify_ssl is True:
            return True
        # cfg.verify_ssl is None → fall through to env-var chain
    return get_ssl_verify()


async def handle_web_fetch(op: WebFetchIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    import httpx

    # FP-0022: Tier 1 handler-level gate — 4-layer approval (config / approvals.yaml
    # / session / interactive). Replaces the catalog-level `web.fetch: allow` gate.
    # `web.fetch: allow` existing config entries continue to pre-approve via Layer 1.
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("web_fetch op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_web_fetch(op.url, ctx.intervention_bus)

    ctx.events.emit("web_fetch_started", url=op.url)
    try:
        # SSL verification — priority: reyn.yaml web.fetch config → env-var chain.
        # See _resolve_ssl_verify() docstring for the full priority order.
        async with httpx.AsyncClient(
            timeout=op.timeout,
            follow_redirects=True,
            headers={"User-Agent": "reyn/1.0"},
            verify=_resolve_ssl_verify(ctx),
        ) as client:
            response = await client.get(op.url)
    except httpx.TimeoutException:
        return {"kind": "web_fetch", "url": op.url, "status": "timeout",
                "error": f"request timed out after {op.timeout}s"}
    except httpx.RequestError as exc:
        return {"kind": "web_fetch", "url": op.url, "status": "error", "error": str(exc)}

    content_type = response.headers.get("content-type", "")

    # Issue #364: when the response is a binary image, switch to
    # bytes + base64 + media_blocks instead of decoding as text. Apply the
    # shared media-size gate (config: multimodal.max_bytes / on_oversize)
    # before allocating any large payloads into the LLM context. Other
    # binary types (audio/video) are deferred — fall through to the text
    # path's errors="replace" behaviour, which preserves the pre-#364
    # output for non-image binary.
    if content_type.startswith("image/"):
        image_bytes = response.content
        if ctx.permission_resolver is not None and ctx.multimodal_config is not None:
            if ctx.intervention_bus is None:
                raise RuntimeError(
                    "web_fetch op requires intervention_bus when loading "
                    "binary media (multimodal gate)"
                )
            try:
                await ctx.permission_resolver.require_media_load(
                    size_bytes=len(image_bytes),
                    source=f"web fetch {op.url}",
                    mime_type=content_type,
                    max_bytes=ctx.multimodal_config.max_bytes,
                    on_oversize=ctx.multimodal_config.on_oversize,
                    bus=ctx.intervention_bus,
                )
            except PermissionError as exc:
                ctx.events.emit(
                    "web_fetch_media_denied",
                    url=op.url, size_bytes=len(image_bytes),
                    mime_type=content_type,
                )
                return {
                    "kind": "web_fetch", "url": op.url, "status": "denied",
                    "content_type": content_type, "size_bytes": len(image_bytes),
                    "error": str(exc),
                }
        # Issue #383 PR-C: emit a path-ref media block when MediaStore is
        # available (= production path). Without a MediaStore (= direct
        # OpContext in tests / legacy callers) fall back to inline base64
        # so the pre-PR-C behaviour is preserved.
        media_block: dict
        if ctx.media_store is not None:
            media_block = ctx.media_store.save_image(
                image_bytes, mime_type=content_type,
                chain_id=ctx.run_id or "", tool="web_fetch", seq=1,
            )
        else:
            import base64
            data_b64 = base64.b64encode(image_bytes).decode("ascii")
            media_block = {
                "type": "image", "data": data_b64, "mimeType": content_type,
            }
        ctx.events.emit(
            "web_fetch_completed",
            url=op.url, status_code=response.status_code,
            content_type=content_type, content_length=len(image_bytes),
            extractor="binary", media_block_count=1,
            stored_as=("path_ref" if ctx.media_store is not None else "inline_b64"),
        )
        return {
            "kind": "web_fetch", "url": op.url, "status": "ok",
            "status_code": response.status_code, "content_type": content_type,
            "content": "", "truncated": False,
            "extractor": "binary",
            "start_index": 0, "next_start": None,
            "total_length": len(image_bytes),
            "media_blocks": [media_block],
        }

    raw = response.text

    if "text/html" in content_type:
        content, extractor_name = _extract_html_text(raw)
    else:
        content = raw
        extractor_name = "none"

    # Pagination (issue #357): slice extracted content by start_index, then
    # cap at max_length. next_start tells the LLM where to resume on the
    # follow-up call. start_index past end-of-content yields empty content
    # with truncated=False.
    total_length = len(content)
    sliced = content[op.start_index:]
    truncated = len(sliced) > op.max_length
    if truncated:
        content = sliced[: op.max_length]
        next_start: int | None = op.start_index + op.max_length
    else:
        content = sliced
        next_start = None

    # #385 PoC: when MediaStore is available, route the (potentially
    # large) extracted text through ``.reyn/tool-results/`` and return a
    # preview + path-ref instead of inlining the full body. The LLM then
    # decides whether the preview is enough or to call ``read_tool_result``
    # for full content. Backward-compat: when ``ctx.media_store is None``
    # (= legacy callers / tests), fall through to the pre-PoC inline shape.
    if ctx.media_store is not None and content:
        saved_block = ctx.media_store.save_tool_result(
            content,
            mime_type=(content_type or "text/plain"),
            chain_id=ctx.run_id or "",
            tool="web_fetch",
            seq=1,
        )
        preview = _generate_web_fetch_preview(
            raw, extracted_text=content, content_type=content_type,
        )
        ctx.events.emit(
            "web_fetch_completed",
            url=op.url,
            status_code=response.status_code,
            content_length=len(content),
            truncated=truncated,
            extractor=extractor_name,
            start_index=op.start_index,
            total_length=total_length,
            stored_as="path_ref",
            path=saved_block.get("path"),
        )
        return {
            "kind": "web_fetch",
            "url": op.url,
            "status": "ok",
            "status_code": response.status_code,
            "content_type": content_type,
            "content": "",
            "preview": preview,
            "path_ref": saved_block,
            "truncated": truncated,
            "extractor": extractor_name,
            "media_blocks": [],
            "start_index": op.start_index,
            "next_start": next_start,
            "total_length": total_length,
            "stored_as": "path_ref",
        }

    ctx.events.emit(
        "web_fetch_completed",
        url=op.url,
        status_code=response.status_code,
        content_length=len(content),
        truncated=truncated,
        extractor=extractor_name,
        start_index=op.start_index,
        total_length=total_length,
    )
    return {
        "kind": "web_fetch",
        "url": op.url,
        "status": "ok",
        "status_code": response.status_code,
        "content_type": content_type,
        "content": content,
        "truncated": truncated,
        "extractor": extractor_name,
        "media_blocks": [],
        "start_index": op.start_index,
        "next_start": next_start,
        "total_length": total_length,
    }


async def handle_web_search(op: WebSearchIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    from reyn.search_backends import get_backend

    # FP-0022: Tier 1 config deny path. web_search is read-only (no side effects),
    # so operator `deny` is the only sensible restriction — no interactive prompt needed.
    if ctx.permission_resolver is not None and ctx.permission_resolver._is_config_denied("web.search"):
        raise PermissionError("web search denied by config (web.search: deny)")

    ctx.events.emit("web_search_started", query=op.query, backend=op.backend)
    try:
        backend = get_backend(op.backend)
        results = await asyncio.to_thread(backend.search, op.query, op.max_results)
    except Exception as exc:
        ctx.events.emit("web_search_failed", query=op.query, backend=op.backend, error=str(exc))
        return {
            "kind": "web_search",
            "query": op.query,
            "backend": op.backend,
            "status": "error",
            "error": str(exc),
        }

    ctx.events.emit("web_search_completed", query=op.query, backend=op.backend, result_count=len(results))
    return {
        "kind": "web_search",
        "query": op.query,
        "backend": op.backend,
        "status": "ok",
        "results": results,
    }


register("web_fetch", handle_web_fetch)
register("web_search", handle_web_search)
