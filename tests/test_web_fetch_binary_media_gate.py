"""Tier 2: web__fetch binary image content + shared media-size gate (issue #364).

This is the first multi-modal cluster PR, so it also lands the shared
infrastructure (= `MultimodalConfig` + `PermissionResolver.require_media_load`)
that #365 (file__read) and #366 (user input) will reuse.

What we pin:
  - Config parsing: defaults + explicit values + bad-type fallback.
  - PermissionResolver.require_media_load: allow / deny / ask branches.
  - handle_web_fetch image path: under-limit returns media_blocks; over-limit
    on_oversize=deny returns status=denied; on_oversize=allow loads the image;
    text-only HTML response is unchanged (= backward compat).

Real PermissionResolver, real OpContext, no collaborator mocks. The
intervention bus uses a fake that pre-answers the prompt without spinning
up the chat-session intervention machinery.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from reyn.config import MultimodalConfig, _build_multimodal_config
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
)

# ── config parsing ─────────────────────────────────────────────────────


def test_multimodal_config_defaults_when_empty() -> None:
    """Tier 2: missing / empty multimodal: section → 5MB cap, ask policy."""
    cfg = _build_multimodal_config(None)
    assert cfg.max_bytes == 5_000_000
    assert cfg.on_oversize == "ask"


def test_multimodal_config_parses_explicit_values() -> None:
    """Tier 2: explicit YAML dict → values flow through."""
    cfg = _build_multimodal_config({"max_bytes": 10_000_000, "on_oversize": "deny"})
    assert cfg.max_bytes == 10_000_000
    assert cfg.on_oversize == "deny"


def test_multimodal_config_rejects_unknown_on_oversize() -> None:
    """Tier 2: invalid on_oversize value falls back to ask (= safest)."""
    cfg = _build_multimodal_config({"on_oversize": "explode"})
    assert cfg.on_oversize == "ask"


def test_multimodal_config_rejects_negative_max_bytes() -> None:
    """Tier 2: negative max_bytes falls back to default (= prevents accidental
    'every image is over the limit' configurations).
    """
    cfg = _build_multimodal_config({"max_bytes": -1})
    assert cfg.max_bytes == 5_000_000


# ── PermissionResolver.require_media_load ──────────────────────────────


class _FakeBus:
    """Drop-in for RequestBus that pre-answers the prompt with `answer`."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(text="", choice_id=self._answer)


def _resolver(tmp_path: Path, *, config: dict | None = None, interactive: bool = True) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=interactive,
    )


def test_gate_passes_when_under_limit(tmp_path) -> None:
    """Tier 2: size <= max_bytes returns without consulting the bus."""
    resolver = _resolver(tmp_path)
    asyncio.run(
        resolver.require_media_load(
            size_bytes=1_000_000,  # well under 5MB
            source="web fetch https://example.com/x.png",
            mime_type="image/png",
            max_bytes=5_000_000,
            on_oversize="ask",
            bus=_FakeBus("never_called"),
        )
    )


def test_gate_allow_skips_prompt_when_over_limit(tmp_path) -> None:
    """Tier 2: on_oversize="allow" lets oversized media through without a
    prompt (= unattended pipeline use case).
    """
    resolver = _resolver(tmp_path)
    asyncio.run(
        resolver.require_media_load(
            size_bytes=10_000_000,
            source="web fetch https://big.example/x.png",
            mime_type="image/png",
            max_bytes=5_000_000,
            on_oversize="allow",
            bus=_FakeBus("never_called"),
        )
    )


def test_gate_deny_raises_when_over_limit(tmp_path) -> None:
    """Tier 2: on_oversize="deny" raises PermissionError without prompting
    (= silent reject for cost-sensitive contexts).
    """
    resolver = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="multimodal.on_oversize=deny"):
        asyncio.run(
            resolver.require_media_load(
                size_bytes=10_000_000,
                source="web fetch https://big.example/x.png",
                mime_type="image/png",
                max_bytes=5_000_000,
                on_oversize="deny",
                bus=_FakeBus("never_called"),
            )
        )


def test_gate_ask_yes_passes(tmp_path) -> None:
    """Tier 2: on_oversize="ask" + user says yes → no exception (= image loads)."""
    resolver = _resolver(tmp_path)
    asyncio.run(
        resolver.require_media_load(
            size_bytes=10_000_000,
            source="web fetch https://big.example/x.png",
            mime_type="image/png",
            max_bytes=5_000_000,
            on_oversize="ask",
            bus=_FakeBus("yes"),
        )
    )


def test_gate_ask_no_raises(tmp_path) -> None:
    """Tier 2: on_oversize="ask" + user says no → PermissionError (=
    image dropped, caller emits status=denied).
    """
    resolver = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="denied by user"):
        asyncio.run(
            resolver.require_media_load(
                size_bytes=10_000_000,
                source="web fetch https://big.example/x.png",
                mime_type="image/png",
                max_bytes=5_000_000,
                on_oversize="ask",
                bus=_FakeBus("no"),
            )
        )


def test_gate_config_layer_preapproves_oversize(tmp_path) -> None:
    """Tier 2: reyn.yaml `media.oversize: allow` pre-approves session-wide;
    Layer 4 prompt never fires.
    """
    resolver = _resolver(
        tmp_path, config={"media.oversize": "allow"}, interactive=False,
    )
    asyncio.run(
        resolver.require_media_load(
            size_bytes=10_000_000,
            source="web fetch https://big.example/x.png",
            mime_type="image/png",
            max_bytes=5_000_000,
            on_oversize="ask",
            bus=_FakeBus("never_called"),
        )
    )


# ── handle_web_fetch image path ────────────────────────────────────────


class _CapturingImageClient:
    """Returns a fixed image/png body for handle_web_fetch end-to-end tests."""

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


def _make_ctx(tmp_path: Path, *, multimodal: MultimodalConfig, bus_answer: str = "yes") -> Any:
    from reyn.data.workspace.workspace import Workspace
    from reyn.events.events import EventLog
    from reyn.op_runtime.context import OpContext

    events = EventLog()
    # Pre-approve the URL-level web.fetch gate via config so these tests
    # exercise ONLY the media-size gate. The URL gate's behaviour is
    # covered by test_web_fetch_unified.py — out of scope here.
    resolver = _resolver(tmp_path, config={"web.fetch": "allow"})
    workspace = Workspace(events=events, permission_resolver=resolver)
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        intervention_bus=_FakeBus(bus_answer),  # type: ignore[arg-type]
        multimodal_config=multimodal,
    )


def test_image_under_limit_returns_media_blocks(tmp_path, monkeypatch) -> None:
    """Tier 2: image/png response under cap → media_blocks carries base64
    payload, content empty, status=ok.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    _CapturingImageClient.body_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200  # ~200 bytes
    _CapturingImageClient.content_type = "image/png"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingImageClient)

    # Pre-approve web_fetch URL gate so we exercise only the media gate.
    ctx = _make_ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        bus_answer="yes",  # if web.fetch prompt fires, accept
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com/foo.png")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["content"] == ""
    assert result["extractor"] == "binary"
    assert result["media_blocks"], "expected at least one media block"
    (block,) = result["media_blocks"]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    # base64 decodes back to the raw image bytes.
    assert base64.b64decode(block["data"]) == _CapturingImageClient.body_bytes


def test_image_over_limit_with_deny_returns_status_denied(tmp_path, monkeypatch) -> None:
    """Tier 2: image > cap + on_oversize=deny → result status=denied, no
    media_blocks, no model-context bloat.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    _CapturingImageClient.body_bytes = b"x" * 10_000_000  # 10MB
    _CapturingImageClient.content_type = "image/png"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingImageClient)

    ctx = _make_ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="deny"),
        bus_answer="yes",
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com/big.png")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "denied"
    assert "media_blocks" not in result or not result.get("media_blocks")
    assert result["size_bytes"] == 10_000_000


def test_image_over_limit_with_ask_no_returns_status_denied(tmp_path, monkeypatch) -> None:
    """Tier 2: image > cap + on_oversize=ask + user says no → status=denied."""
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    _CapturingImageClient.body_bytes = b"x" * 10_000_000
    _CapturingImageClient.content_type = "image/jpeg"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingImageClient)

    ctx = _make_ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        bus_answer="no",
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com/big.jpg")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "denied"


def test_image_over_limit_with_allow_returns_media_blocks(tmp_path, monkeypatch) -> None:
    """Tier 2: image > cap + on_oversize=allow → loads anyway, no prompt."""
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    _CapturingImageClient.body_bytes = b"x" * 10_000_000
    _CapturingImageClient.content_type = "image/png"
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingImageClient)

    ctx = _make_ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="allow"),
        bus_answer="never_called",
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com/big.png")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["media_blocks"], "expected at least one media block"


def test_html_response_unchanged_when_image_gate_present(tmp_path, monkeypatch) -> None:
    """Tier 2: text/html response → media_blocks is empty list, content is
    extracted text. Backward compat with #355 / #357 paths preserved.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    class _HTMLClient:
        def __init__(self, **kwargs: Any) -> None:
            self._response = httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><body><p>hello world</p></body></html>",
                request=httpx.Request("GET", "https://example.com"),
            )
        async def __aenter__(self) -> "_HTMLClient":
            return self
        async def __aexit__(self, *args: object) -> None: pass
        async def get(self, url: str) -> httpx.Response: return self._response

    monkeypatch.setattr(httpx, "AsyncClient", _HTMLClient)

    ctx = _make_ctx(
        tmp_path,
        multimodal=MultimodalConfig(max_bytes=5_000_000, on_oversize="ask"),
        bus_answer="yes",
    )
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    result = asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert result["status"] == "ok"
    assert result["media_blocks"] == []
    assert "hello world" in result["content"]
    assert result["extractor"] in {"trafilatura", "stdlib"}
