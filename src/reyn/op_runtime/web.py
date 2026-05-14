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
    raw = response.text

    if "text/html" in content_type:
        extractor = _TextExtractor()
        extractor.feed(raw)
        content = extractor.text()
    else:
        content = raw

    truncated = len(content) > op.max_length
    if truncated:
        content = content[: op.max_length]

    ctx.events.emit(
        "web_fetch_completed",
        url=op.url,
        status_code=response.status_code,
        content_length=len(content),
        truncated=truncated,
    )
    return {
        "kind": "web_fetch",
        "url": op.url,
        "status": "ok",
        "status_code": response.status_code,
        "content_type": content_type,
        "content": content,
        "truncated": truncated,
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
