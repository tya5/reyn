"""Tests for #2597 slice ④ — MCP OAuth 2.1 + Streamable HTTP completion.

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``
on collaborators. The full browser-based OAuth Authorization Code Grant +
PKCE round-trip needs a real authorization server and a human to click
"Allow" — that is a manual/dogfood step (see the PR's Test plan), NOT
something a unit test fakes. These tests instead exercise the real WIRING
with real components: real ``load_config``, the real
``fastmcp.client.auth.OAuth`` object (built and inspected, never invoked),
the real ``StreamableHttpTransport``, and real file I/O against a tmp
``oauth_tokens.json``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient, MCPError
from reyn.mcp.oauth_token_storage import MCPOAuthTokenStorage, has_stored_token


@pytest.fixture
def oauth_store_path(tmp_path, monkeypatch) -> Path:
    """Per-test OAuth token store — mirrors the FP-0016 fixture pattern in
    test_fp0016_b_oauth_refresh.py (same env-var override, same file shape)."""
    p = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(p))
    return p


# ── (a) config parses + validates via the real load_config ────────────────


def test_oauth_server_config_parses_via_load_config(tmp_path, monkeypatch) -> None:
    """Tier 1: an ``auth: oauth`` MCP server entry survives the real
    reyn.yaml -> load_config -> ReynConfig.mcp round trip intact, including
    the nested scopes/client_id fields."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    github:\n"
        "      type: http\n"
        "      url: https://api.githubcopilot.com/mcp/\n"
        "      auth:\n"
        "        type: oauth\n"
        "        scopes: [repo, read:org]\n"
        "        client_id: my-client-id\n",
        encoding="utf-8",
    )
    from reyn.config.loader import load_config

    cfg = load_config(cwd=tmp_path)
    server_cfg = cfg.mcp["servers"]["github"]
    assert server_cfg["type"] == "http"
    assert server_cfg["auth"]["type"] == "oauth"
    assert server_cfg["auth"]["scopes"] == ["repo", "read:org"]
    assert server_cfg["auth"]["client_id"] == "my-client-id"

    # And the parsed dict, fed straight into MCPClient, builds an OAuth
    # transport auth object without raising (this client never connects).
    client = MCPClient(dict(server_cfg), non_interactive=False)
    transport = client._open_transport()
    from fastmcp.client.auth import OAuth

    assert isinstance(transport.auth, OAuth)


def test_bare_oauth_string_shorthand_parses() -> None:
    """Tier 1: ``auth: oauth`` (bare string) is shorthand for ``{"type": "oauth"}``."""
    cfg = {"type": "http", "url": "https://example.com/mcp", "auth": "oauth"}
    client = MCPClient(cfg, non_interactive=False)
    transport = client._open_transport()
    from fastmcp.client.auth import OAuth

    assert isinstance(transport.auth, OAuth)
    assert transport.auth.mcp_url == "https://example.com/mcp"


def test_unsupported_auth_type_rejected() -> None:
    """Tier 1: a non-'oauth' auth.type is a clear config error, not a silent no-op."""
    cfg = {"type": "http", "url": "https://example.com/mcp", "auth": {"type": "saml"}}
    client = MCPClient(cfg, non_interactive=False)
    with pytest.raises(MCPError, match="saml"):
        client._open_transport()


def test_auth_on_stdio_server_rejected_at_construction() -> None:
    """Tier 1: OAuth is meaningless over stdio — reject eagerly, don't silently ignore."""
    with pytest.raises(ValueError, match="stdio"):
        MCPClient({"type": "stdio", "command": "x", "auth": "oauth"})


def test_auth_on_sse_server_rejected_at_construction() -> None:
    """Tier 1: same restriction for sse — OAuth only wired for Streamable HTTP."""
    with pytest.raises(ValueError, match="sse"):
        MCPClient({"type": "sse", "url": "https://x/sse", "auth": "oauth"})


# ── (b) TokenStorage round-trips through oauth_tokens.json (outside bucket) ─


def test_token_storage_round_trips_through_outside_bucket_file(oauth_store_path) -> None:
    """Tier 2: MCPOAuthTokenStorage persists + reloads a token through the
    real oauth_tokens.json file, 0600-permissioned, keyed per server URL —
    the SAME on-disk file reyn.security.secrets.oauth's device-grant store
    uses (outside bucket, per reyn-dir-layout.md), never a private in-memory
    dict a test would need to reach into."""
    storage_a = MCPOAuthTokenStorage(path=oauth_store_path)
    non_default_value = {
        "access_token": "at-nondefault-9f3c",
        "refresh_token": "rt-nondefault-2b71",
        "token_type": "Bearer",
    }

    async def _round_trip() -> None:
        await storage_a.put(
            "https://server-a.example.com/tokens",
            non_default_value,
            collection="mcp-oauth-token",
        )

    asyncio.run(_round_trip())

    assert oauth_store_path.exists()
    assert oct(oauth_store_path.stat().st_mode & 0o777) == "0o600"

    # A FRESH storage instance (same path) reads back the same value —
    # proves it round-trips through the file, not an in-process cache.
    storage_b = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _reload() -> dict:
        return await storage_b.get(
            "https://server-a.example.com/tokens", collection="mcp-oauth-token"
        )

    reloaded = asyncio.run(_reload())
    assert reloaded == non_default_value


def test_token_storage_per_server_keying_does_not_collide(oauth_store_path) -> None:
    """Tier 2: two different server URLs under the same collection are
    stored independently — writing server B's token must not disturb
    server A's (per-server keying invariant)."""
    storage = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _write_both() -> None:
        await storage.put(
            "https://server-a.example.com/tokens",
            {"access_token": "token-a-4471"},
            collection="mcp-oauth-token",
        )
        await storage.put(
            "https://server-b.example.com/tokens",
            {"access_token": "token-b-8823"},
            collection="mcp-oauth-token",
        )

    asyncio.run(_write_both())

    async def _read_both():
        a = await storage.get(
            "https://server-a.example.com/tokens", collection="mcp-oauth-token"
        )
        b = await storage.get(
            "https://server-b.example.com/tokens", collection="mcp-oauth-token"
        )
        return a, b

    a, b = asyncio.run(_read_both())
    assert a == {"access_token": "token-a-4471"}
    assert b == {"access_token": "token-b-8823"}


def test_has_stored_token_reflects_real_store_state(oauth_store_path) -> None:
    """Tier 2: has_stored_token() is a thin, real (non-mocked) read of the
    same file MCPOAuthTokenStorage writes — used by the headless pre-flight
    check in client.py."""
    url = "https://server-c.example.com/mcp"
    assert has_stored_token(url, path=oauth_store_path) is False

    storage = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _write() -> None:
        # Mirrors FastMCP's own TokenStorageAdapter key shape exactly
        # (f"{url.rstrip('/')}/tokens", collection "mcp-oauth-token") —
        # verified against the installed fastmcp 3.4.2 source.
        await storage.put(
            f"{url}/tokens",
            {"access_token": "at-real-6620"},
            collection="mcp-oauth-token",
        )

    asyncio.run(_write())
    assert has_stored_token(url, path=oauth_store_path) is True


def test_expired_token_entry_not_reported_as_stored(oauth_store_path) -> None:
    """Tier 2: a TTL'd-out entry must not be reported as a usable token —
    has_stored_token() checks the real (not-a-private-field) expiry the
    entry carries."""
    url = "https://server-d.example.com/mcp"
    storage = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _write_expired() -> None:
        await storage.put(
            f"{url}/tokens",
            {"access_token": "at-expiring-3391"},
            collection="mcp-oauth-token",
            ttl=-1,  # already expired
        )

    asyncio.run(_write_expired())
    assert has_stored_token(url, path=oauth_store_path) is False


# ── (c) an OAuth-configured server builds the right transport/auth object ──


def test_http_oauth_server_builds_real_oauth_transport_auth(oauth_store_path) -> None:
    """Tier 1: inspect the REAL fastmcp.client.auth.OAuth object attached to
    the REAL StreamableHttpTransport (mirrors the ②a header tests that
    inspect the real StreamableHttpTransport) — proves the config->transport
    wiring, not a hand-rolled fake. Uses only PUBLIC surface: ``mcp_url``,
    the public ``context.client_metadata.scope`` (never the leading-
    underscore ``_scopes``/``_client_id`` ctor-cache fields), and a real
    round trip through the public ``token_storage_adapter.get_tokens()`` /
    ``set_tokens()`` to prove the storage binding — never reaching into
    ``token_storage_adapter``'s private ``_key_value_store``."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": {
            "type": "oauth",
            "scopes": ["a", "b"],
            "client_id": "client-123",
            "client_secret": "secret-456",
        },
    }
    client = MCPClient(cfg, non_interactive=False)
    transport = client._open_transport()

    from fastmcp.client.auth import OAuth

    assert isinstance(transport.auth, OAuth)
    assert transport.auth.mcp_url == "https://mcp.example.com/mcp"
    # public dataclass field on the public `context` attribute
    assert transport.auth.context.client_metadata.scope == "a b"

    # Prove the OAuth object's storage really is bound to OUR
    # MCPOAuthTokenStorage / oauth_store_path — round-trip a token through
    # the built object's OWN public token_storage_adapter, then confirm a
    # separate MCPOAuthTokenStorage pointed at the same path sees it.
    from mcp.shared.auth import OAuthToken

    async def _round_trip() -> None:
        await transport.auth.token_storage_adapter.set_tokens(
            OAuthToken(access_token="at-wiring-proof-5510", token_type="Bearer")
        )

    asyncio.run(_round_trip())

    independent_storage = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _read_independently() -> dict:
        return await independent_storage.get(
            "https://mcp.example.com/mcp/tokens", collection="mcp-oauth-token"
        )

    seen = asyncio.run(_read_independently())
    assert seen is not None
    assert seen["access_token"] == "at-wiring-proof-5510"


# ── (d) static bearer / header auth regression — unaffected by slice ④ ─────


def test_static_bearer_header_auth_unaffected(oauth_store_path) -> None:
    """Tier 1: pre-④ static bearer auth (headers, no 'auth' key at all) still
    builds a transport with auth=None and the header intact — the ④ wiring
    is additive, never a behavior change for the existing header-auth path."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer static-token-abc"},
    }
    client = MCPClient(cfg)
    transport = client._open_transport()
    assert transport.auth is None
    assert transport.headers["Authorization"] == "Bearer static-token-abc"


# ── (e) headless + no stored token -> clear MCPError, never a hang ─────────


def test_headless_no_token_raises_clear_mcp_error_not_hang(oauth_store_path) -> None:
    """Tier 1: a non-interactive caller with no cached token gets a clear,
    immediate MCPError instead of FastMCP opening a browser + waiting on a
    localhost callback nobody can complete."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": {"type": "oauth"},
    }
    client = MCPClient(cfg, non_interactive=True)
    with pytest.raises(MCPError, match="requires OAuth authentication"):
        client._open_transport()


def test_headless_with_stored_token_proceeds(oauth_store_path) -> None:
    """Tier 1: once a token IS cached for this exact server URL, the same
    non-interactive client builds the transport without raising — the
    pre-flight check is scoped to "no token yet", not "always block
    headless"."""
    url = "https://mcp.example.com/mcp"
    storage = MCPOAuthTokenStorage(path=oauth_store_path)

    async def _seed() -> None:
        await storage.put(
            f"{url}/tokens",
            {"access_token": "at-cached-7742"},
            collection="mcp-oauth-token",
        )

    asyncio.run(_seed())

    cfg = {"type": "http", "url": url, "auth": {"type": "oauth"}}
    client = MCPClient(cfg, non_interactive=True)
    transport = client._open_transport()  # must not raise
    from fastmcp.client.auth import OAuth

    assert isinstance(transport.auth, OAuth)


def test_interactive_client_with_no_token_does_not_raise_preflight_error(
    oauth_store_path,
) -> None:
    """Tier 1: an explicitly interactive client (non_interactive=False) is
    allowed to proceed to FastMCP's own browser flow even with no cached
    token — the headless guard only fires for non-interactive callers."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": {"type": "oauth"},
    }
    client = MCPClient(cfg, non_interactive=False)
    transport = client._open_transport()  # must not raise
    from fastmcp.client.auth import OAuth

    assert isinstance(transport.auth, OAuth)


# ── never write OAuth tokens into any rewind/recovery-core path ────────────


def test_oauth_tokens_never_land_under_dot_reyn_recovery_core(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: OS invariant — OAuth tokens are OUTSIDE-bucket data (per
    reyn-dir-layout.md), never written under a project's ``.reyn/state/`` or
    ``.reyn/config/`` (the recovery-core, WAL/rewind-reconstructed subtrees).
    This test asserts the NEGATIVE directly: after writing a token via the
    real storage, no file appears anywhere under a project ``.reyn/`` tree,
    because MCPOAuthTokenStorage never resolves its path there — the write
    path is fully independent of any project root / WAL / config-generation
    machinery. truncate-falsify N/A here (no recovery-core, hard-rule gate
    only applies to reconstructable state; this is intentionally NOT
    reconstructable — see the module docstring)."""
    project_reyn_dir = tmp_path / "project" / ".reyn"
    project_reyn_dir.mkdir(parents=True)
    oauth_path = tmp_path / "outside-bucket" / "oauth_tokens.json"
    storage = MCPOAuthTokenStorage(path=oauth_path)

    async def _write() -> None:
        await storage.put(
            "https://mcp.example.com/tokens",
            {"access_token": "at-isolation-check-1123"},
            collection="mcp-oauth-token",
        )

    asyncio.run(_write())

    assert oauth_path.exists()
    # Nothing was written anywhere under the project's .reyn/ tree.
    written_under_dot_reyn = list(project_reyn_dir.rglob("*"))
    assert written_under_dot_reyn == []
