"""Tier 2: OS invariant — ``mcp.servers.<name>.headers`` field (FP-0016 Component A).

Verifies the end-to-end path from yaml on disk through ``load_config`` into
``ReynConfig.mcp`` and on to the HTTP transport, including ``${VAR}`` env
interpolation (ADR-0030).

Component A scope:
  - ``headers: dict[str, str]`` is accepted on http-mode MCP server configs.
  - ``${VAR}`` tokens inside header values resolve at config-load time.
  - The headers dict reaches the real ``StreamableHttpTransport`` verbatim
    (post-expand) — #2597 S1, updated for #4282's fastmcp-to-official-SDK
    migration: inspected via the kwargs the real ``streamablehttp_client``
    call site is invoked with (captured at that boundary, not a mocked SDK
    entry point — see ``_capture_streamablehttp_client_kwargs`` below).
  - Missing / empty ``headers`` is fine — no header is sent (back-compat).
"""
from __future__ import annotations

from pathlib import Path

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
                    "type": "streamable-http",
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
    assert gh["type"] == "streamable-http"
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
                    "type": "streamable-http",
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
# Transport boundary: headers reach the real StreamableHttpTransport verbatim
# ---------------------------------------------------------------------------


def _capture_streamablehttp_client_kwargs(monkeypatch) -> dict:
    """#4282: the pre-#4282 form of the two tests below inspected the
    constructed ``fastmcp.client.transports.StreamableHttpTransport``
    object's ``.headers``/``.url`` directly (via the now-removed
    ``_open_transport``); that helper no longer exists —
    ``_initialize_http_or_sse`` calls ``mcp.client.streamable_http.
    streamablehttp_client(url, headers=..., ...)`` inline instead. Captures
    the REAL function's kwargs by wrapping it (still calls through to the
    real one, so the connection attempt proceeds exactly as it would
    unmodified) rather than faking the SDK — same seam
    ``test_mcp_client_stderr_capture.py``'s stdio_client patch already
    established, applied to the http transport function instead.

    #4412 pin-bump PR: ``streamablehttp_client`` (headers/timeout kwargs
    directly) is GONE on mcp 2.0 — its replacement, ``streamable_http_client``,
    takes a pre-built HTTP client instead, and ``client.py`` now builds that
    client via the SDK's own ``create_mcp_http_client(headers=...)`` factory
    (see that call site's own comment for why: reyn deliberately does not
    import the SDK's transport-library type by name). Headers therefore now
    reach ``create_mcp_http_client``, not ``streamable_http_client`` itself —
    wrap BOTH functions to keep capturing url (from the latter) and headers
    (from the former)."""
    import mcp.client.streamable_http as sh_mod

    captured: dict = {}
    real_create_client = sh_mod.create_mcp_http_client
    real_transport = sh_mod.streamable_http_client

    def _capturing_create_client(headers=None, timeout=None, auth=None):
        captured["headers"] = headers
        return real_create_client(headers=headers, timeout=timeout, auth=auth)

    def _capturing_transport(url, **kwargs):
        captured["url"] = url
        return real_transport(url, **kwargs)

    monkeypatch.setattr(sh_mod, "create_mcp_http_client", _capturing_create_client)
    monkeypatch.setattr(sh_mod, "streamable_http_client", _capturing_transport)
    return captured


def test_mcp_headers_reach_http_transport(monkeypatch) -> None:
    """Tier 2: framework boundary — a config with resolved headers reaches the
    real ``streamablehttp_client`` call with the exact post-expansion header
    dict. This pins the contract: whatever the caller puts in
    ``cfg['headers']``, MCPClient passes through unfiltered/unrewritten. The
    target host doesn't exist, so ``initialize()`` fails at connect time —
    the headers are already captured by then.
    """
    import asyncio

    from reyn.mcp.client import MCPClient, MCPError

    captured = _capture_streamablehttp_client_kwargs(monkeypatch)
    cfg = {
        "type": "streamable-http",
        "url": "https://api.example.com/mcp",
        "headers": {
            "Authorization": "Bearer abc123",
            "X-API-Version": "2024-01-01",
        },
        "timeout": 45,
        "init_timeout": 1,
    }

    client = MCPClient(cfg)
    try:
        asyncio.run(client.initialize())
    except MCPError:
        pass  # expected — api.example.com is not a real MCP server
    finally:
        asyncio.run(client.close())

    assert captured["url"] == "https://api.example.com/mcp"
    assert captured["headers"] == {
        "Authorization": "Bearer abc123",
        "X-API-Version": "2024-01-01",
    }


def test_mcp_headers_default_empty_when_omitted(monkeypatch) -> None:
    """Tier 2: framework boundary — an http MCP config without ``headers``
    reaches ``streamablehttp_client`` with an empty header dict (no
    spurious headers injected)."""
    import asyncio

    from reyn.mcp.client import MCPClient, MCPError

    captured = _capture_streamablehttp_client_kwargs(monkeypatch)
    cfg = {"type": "streamable-http", "url": "http://x/mcp", "init_timeout": 1}
    client = MCPClient(cfg)
    try:
        asyncio.run(client.initialize())
    except MCPError:
        pass
    finally:
        asyncio.run(client.close())

    assert captured["headers"] == {}
