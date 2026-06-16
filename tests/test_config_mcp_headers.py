"""Tier 2: OS invariant — ``mcp.servers.<name>.headers`` field (FP-0016 Component A).

Verifies the end-to-end path from yaml on disk through ``load_config`` into
``ReynConfig.mcp`` and on to the HTTP transport, including ``${VAR}`` env
interpolation (ADR-0030).

Component A scope:
  - ``headers: dict[str, str]`` is accepted on http-mode MCP server configs.
  - ``${VAR}`` tokens inside header values resolve at config-load time.
  - The headers dict reaches ``streamablehttp_client`` verbatim (post-expand).
  - Missing / empty ``headers`` is fine — no header is sent (back-compat).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Config-load: headers field round-trips through ReynConfig.mcp
# ---------------------------------------------------------------------------


def test_mcp_headers_field_load_with_env_interpolation(tmp_path, monkeypatch):
    """Tier 2: load_config() preserves ``mcp.servers.<name>.headers`` and resolves
    ``${VAR}`` tokens in header values via ADR-0030 ``expand_env``.

    Mirrors the FP-0016 sample yaml: a github-style HTTP MCP server with
    ``Authorization: Bearer ${GITHUB_TOKEN}`` and a static ``X-API-Version``.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_t0pSecret")
    # Avoid pollution from the developer's real ~/.reyn/secrets.env loader.
    monkeypatch.setattr(
        "reyn.security.secrets.loader.load_secrets_to_environ", lambda *a, **k: None
    )

    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {
        "model": "standard",
        "mcp": {
            "servers": {
                "github": {
                    "type": "http",
                    "url": "https://api.githubcopilot.com/mcp/",
                    "headers": {
                        "Authorization": "Bearer ${GITHUB_TOKEN}",
                        "X-API-Version": "2024-01-01",
                    },
                },
            },
        },
    })
    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    cfg = load_config(tmp_path)
    servers = cfg.mcp.get("servers") or {}
    assert "github" in servers, "github MCP server config should round-trip"
    gh = servers["github"]
    assert gh["type"] == "http"
    assert gh["url"] == "https://api.githubcopilot.com/mcp/"
    # ${VAR} resolves at load time
    assert gh["headers"]["Authorization"] == "Bearer ghp_t0pSecret"
    # Non-interpolated header passes through unchanged
    assert gh["headers"]["X-API-Version"] == "2024-01-01"


def test_mcp_headers_optional_back_compat(tmp_path, monkeypatch):
    """Tier 2: omitting ``headers`` is valid (back-compat: pre-FP-0016 configs
    without the field continue to load and run)."""
    monkeypatch.setattr(
        "reyn.security.secrets.loader.load_secrets_to_environ", lambda *a, **k: None
    )

    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {
        "model": "standard",
        "mcp": {
            "servers": {
                "local": {
                    "type": "http",
                    "url": "http://localhost:3000/mcp",
                },
            },
        },
    })
    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    cfg = load_config(tmp_path)
    local = cfg.mcp["servers"]["local"]
    assert local["url"] == "http://localhost:3000/mcp"
    assert "headers" not in local or local["headers"] in (None, {})


# ---------------------------------------------------------------------------
# Transport boundary: headers reach streamablehttp_client verbatim
# ---------------------------------------------------------------------------


@pytest.fixture()
def _patched_mcp_sdk():
    """Patch MCP SDK transport entry-points used by MCPClient.

    Intentional SDK patch — admitted per tier-audit HTTP-transport exemption
    (same pattern as ``patched_sdk`` in ``tests/test_mcp_client.py``).
    ``streamablehttp_client`` and ``ClientSession`` are 3rd-party SDK boundaries
    that cannot be replaced with LLMReplay; the fake functions defined at module
    level below are injected here and captured via the ``_http_captured`` dict
    passed to each test through the ``captured`` parameter.

    Deferral note: converting these patches to a proper LLMReplay-compatible
    Fake requires extending LLMReplay to cover the MCP SDK transport layer —
    tracked as a follow-up, not in scope for this PR.
    """
    captured: dict = {}

    @asynccontextmanager
    async def _fake_http_client(url, headers=None, timeout=30):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["timeout"] = timeout
        yield ("read", "write", lambda: None)

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def initialize(self):
            return None

    with mock.patch(
        "mcp.client.streamable_http.streamablehttp_client", _fake_http_client
    ), mock.patch("mcp.ClientSession", _FakeSession):
        yield captured


def test_mcp_headers_reach_http_transport(_patched_mcp_sdk):
    """Tier 2: framework boundary — a config with resolved headers reaches the
    ``streamablehttp_client`` call with the exact post-expansion header dict.

    This pins the contract: whatever the caller puts in ``cfg['headers']``,
    MCPClient passes through to the SDK — no filtering, no rewriting.
    """
    captured = _patched_mcp_sdk

    from reyn.mcp_client import MCPClient

    cfg = {
        "type": "http",
        "url": "https://api.example.com/mcp",
        "headers": {
            "Authorization": "Bearer abc123",
            "X-API-Version": "2024-01-01",
        },
        "timeout": 45,
    }

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        await client.close()

    asyncio.run(_run_it())

    assert captured["url"] == "https://api.example.com/mcp"
    assert captured["headers"] == {
        "Authorization": "Bearer abc123",
        "X-API-Version": "2024-01-01",
    }
    assert captured["timeout"] == 45


def test_mcp_headers_default_empty_when_omitted(_patched_mcp_sdk):
    """Tier 2: framework boundary — an http MCP config without ``headers`` yields
    an empty header dict at the transport (no spurious headers injected)."""
    captured = _patched_mcp_sdk

    from reyn.mcp_client import MCPClient

    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        await client.close()

    asyncio.run(_run_it())
    assert captured["headers"] == {}
