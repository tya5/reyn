"""Tier 2: web_fetch SSL config — declarative SSL via reyn.yaml (FP-0022 follow-up).

Verifies that handle_web_fetch and RegistryClient pass the correct ``verify``
value to httpx.AsyncClient, based on the priority order:
  1. web.fetch.ca_bundle (str path) → verify=<path>
  2. web.fetch.verify_ssl: false    → verify=False
  3. web.fetch.verify_ssl: true     → verify=True
  4. neither set (None)             → litellm.get_ssl_verify() env-var fallback

No unittest.mock. All helpers use real instances or httpx.MockTransport.
httpx.AsyncClient is monkeypatched at the class level to capture constructor
kwargs — this is a structural invariant (the right layer is called), not a
behavioral assertion on httpx internals.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest import mock  # only for cache_mod patching — no collaborator mocks

import httpx
import pytest

from reyn.config import WebConfig, WebFetchConfig, _build_web_config

# ---------------------------------------------------------------------------
# Helpers to build a minimal OpContext for web_fetch handler tests
# ---------------------------------------------------------------------------

def _make_ctx(web_config: WebConfig | None = None, tmp_path: Path | None = None):
    """Build a minimal OpContext for testing handle_web_fetch.

    Uses real EventLog and Workspace stubs — no mock collaborators.
    """
    import tempfile

    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    # EventLog stub — only needs emit().
    class _FakeEventLog:
        subscribers: list = []
        def emit(self, *args, **kwargs) -> None:
            pass

    # Workspace stub — handle_web_fetch does not call workspace methods.
    class _FakeWorkspace:
        pass

    return OpContext(
        workspace=_FakeWorkspace(),  # type: ignore[arg-type]
        events=_FakeEventLog(),      # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,    # no permission gate in these tests
        web_config=web_config,
    )


# ---------------------------------------------------------------------------
# Helper: capture the verify kwarg passed to httpx.AsyncClient.__init__
# ---------------------------------------------------------------------------

class _CaptureAsyncClient:
    """Drop-in replacement for httpx.AsyncClient that records __init__ kwargs
    and returns a canned success response.

    Used as a context manager to match the ``async with httpx.AsyncClient(...)``
    pattern in handle_web_fetch.
    """

    captured_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _CaptureAsyncClient.captured_kwargs = dict(kwargs)
        self._response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"hello",
            request=httpx.Request("GET", "https://example.com"),
        )

    async def __aenter__(self) -> "_CaptureAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get(self, url: str) -> httpx.Response:
        return self._response


# ---------------------------------------------------------------------------
# 1. ca_bundle config → verify=<path>
# ---------------------------------------------------------------------------

def test_verify_ssl_ca_bundle_in_config_passes_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: web.fetch.ca_bundle set → handle_web_fetch passes verify=<path> to httpx.

    Invariant: when reyn.yaml supplies web.fetch.ca_bundle, the CA bundle path
    is forwarded to httpx.AsyncClient(verify=...) so requests are validated
    against the custom CA (corporate MITM proxy / private PKI use case).
    """
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    cfg = WebConfig(fetch=WebFetchConfig(ca_bundle="/etc/ssl/certs/corp-ca.pem"))
    ctx = _make_ctx(web_config=cfg)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureAsyncClient)
    asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert _CaptureAsyncClient.captured_kwargs.get("verify") == "/etc/ssl/certs/corp-ca.pem"


# ---------------------------------------------------------------------------
# 2. verify_ssl: false → verify=False
# ---------------------------------------------------------------------------

def test_verify_ssl_false_in_config_passes_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: web.fetch.verify_ssl: false → handle_web_fetch passes verify=False to httpx.

    Invariant: when reyn.yaml sets web.fetch.verify_ssl to False, SSL
    certificate validation is disabled in httpx (controlled environment use).
    ca_bundle takes priority over verify_ssl; this test has no ca_bundle set.
    """
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    cfg = WebConfig(fetch=WebFetchConfig(verify_ssl=False))
    ctx = _make_ctx(web_config=cfg)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureAsyncClient)
    asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert _CaptureAsyncClient.captured_kwargs.get("verify") is False


# ---------------------------------------------------------------------------
# 3. verify_ssl: true → verify=True
# ---------------------------------------------------------------------------

def test_verify_ssl_true_in_config_passes_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: web.fetch.verify_ssl: true → handle_web_fetch passes verify=True to httpx.

    Invariant: explicit true forces SSL verification regardless of env vars.
    """
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    cfg = WebConfig(fetch=WebFetchConfig(verify_ssl=True))
    ctx = _make_ctx(web_config=cfg)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureAsyncClient)
    asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert _CaptureAsyncClient.captured_kwargs.get("verify") is True


# ---------------------------------------------------------------------------
# 4. ca_bundle takes priority over verify_ssl when both are set
# ---------------------------------------------------------------------------

def test_ca_bundle_takes_priority_over_verify_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: ca_bundle config takes priority over verify_ssl when both are set.

    Invariant: the ca_bundle path is the highest-priority SSL knob. Setting
    verify_ssl: false alongside ca_bundle must not override the bundle path.
    """
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    cfg = WebConfig(fetch=WebFetchConfig(
        ca_bundle="/corp/ca.pem",
        verify_ssl=False,  # must NOT win — ca_bundle takes priority
    ))
    ctx = _make_ctx(web_config=cfg)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureAsyncClient)
    asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    assert _CaptureAsyncClient.captured_kwargs.get("verify") == "/corp/ca.pem"


# ---------------------------------------------------------------------------
# 5. env-var fallback when config is unset (web_config=None)
# ---------------------------------------------------------------------------

def test_env_var_fallback_when_config_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: SSL_VERIFY env var is respected when web_config is None.

    Invariant: when no web config is supplied (config unset), the handler
    falls through to litellm.get_ssl_verify() — which reads SSL_VERIFY from
    the environment. This preserves the existing env-var behavior so there is
    no behavioral regression for users who rely on SSL_VERIFY.

    We verify that the verify value passed to httpx matches what
    litellm.get_ssl_verify() returns directly — both must agree.
    """
    from litellm.llms.custom_httpx.http_handler import get_ssl_verify

    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp

    # Set SSL_VERIFY=0 so get_ssl_verify() returns a non-True value.
    monkeypatch.setenv("SSL_VERIFY", "0")

    ctx = _make_ctx(web_config=None)  # no config → env-var fallback path
    op = WebFetchIROp(kind="web_fetch", url="https://example.com", max_length=50_000)

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureAsyncClient)
    asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))

    # The verify value forwarded to httpx must match what litellm.get_ssl_verify()
    # returns in the same env. We do not pin the exact type/value here because
    # litellm's return type (bool vs str) is an internal litellm detail.
    expected = get_ssl_verify()
    assert _CaptureAsyncClient.captured_kwargs.get("verify") == expected


# ---------------------------------------------------------------------------
# 6. _build_web_config parses reyn.yaml web: section correctly
# ---------------------------------------------------------------------------

def test_build_web_config_ca_bundle_and_verify_ssl() -> None:
    """Tier 2: _build_web_config() correctly parses web.fetch.ca_bundle and verify_ssl.

    Invariant: the config parser produces the correct WebFetchConfig from a
    raw YAML dict so that end-to-end config loading (reyn.yaml → WebConfig)
    is structurally correct.
    """
    raw = {
        "fetch": {
            "ca_bundle": "/corp/ca.pem",
            "verify_ssl": False,
        }
    }
    cfg = _build_web_config(raw)
    assert cfg.fetch.ca_bundle == "/corp/ca.pem"
    assert cfg.fetch.verify_ssl is False


def test_build_web_config_defaults_when_empty() -> None:
    """Tier 2: _build_web_config() returns full defaults when section is absent."""
    cfg = _build_web_config({})
    assert cfg.fetch.ca_bundle is None
    assert cfg.fetch.verify_ssl is None


def test_build_web_config_defaults_when_none() -> None:
    """Tier 2: _build_web_config() returns full defaults when raw is None."""
    cfg = _build_web_config(None)
    assert cfg.fetch.ca_bundle is None
    assert cfg.fetch.verify_ssl is None


# ---------------------------------------------------------------------------
# 7. RegistryClient verify constructor arg
# ---------------------------------------------------------------------------

def test_registry_client_verify_false_passed_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: RegistryClient(verify=False) passes verify=False to httpx.AsyncClient.

    Invariant: the RegistryClient's verify constructor arg is forwarded to
    httpx so MCP registry requests skip SSL validation when configured.
    """
    from reyn.core.registry.client import RegistryClient

    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

        async def __aenter__(self) -> "_CapturingClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)

    async def _run() -> None:
        client = RegistryClient(verify=False)
        async with client:
            pass

    asyncio.run(_run())
    assert captured.get("verify") is False


def test_registry_client_ca_bundle_passed_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: RegistryClient(verify='/path/ca.pem') passes that path to httpx.

    Invariant: string verify (= CA bundle path) flows from constructor to httpx
    so registry requests use the corporate CA bundle.
    """
    from reyn.core.registry.client import RegistryClient

    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

        async def __aenter__(self) -> "_CapturingClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)

    async def _run() -> None:
        client = RegistryClient(verify="/corp/ca.pem")
        async with client:
            pass

    asyncio.run(_run())
    assert captured.get("verify") == "/corp/ca.pem"
