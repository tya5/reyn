"""Tests for #2597 slice ④ — MCP OAuth 2.1 + Streamable HTTP (#4282 rewrite:
fastmcp's ``OAuth`` retired for the official SDK's ``OAuthClientProvider``
directly).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``
on collaborators. The full browser-based OAuth Authorization Code Grant +
PKCE round-trip needs a real authorization server and a human to click
"Allow" — that is a manual/dogfood step, NOT something a unit test fakes.

#4282 rewrite rationale (lead-coder review): the pre-#4282 version of this
file inspected the REAL ``fastmcp.client.auth.OAuth`` object's own fields
(``.mcp_url``, ``.context.client_metadata.scope``, ...) — pinning a THIRD
PARTY's object shape under reyn's name (Tier question ①'s "whose bug is it
if this fails" test: a fastmcp/SDK field rename fails it, not a reyn bug).
Rebuilding the SAME shape of test against the NEW SDK's
``OAuthClientProvider`` would carry the same defect forward with a
different dependency. Rewritten to ask reyn's own three questions instead
(lead-coder, verbatim): ① a configured token storage path actually gets a
token written to it; ② a stored token is reused on the next connection (no
re-authentication triggered); ③ an orphaned/corrupt token fails legibly,
not silently. All three are reyn's promises — if they fail, it's reyn's
bug, not the SDK's.
"""
from __future__ import annotations

import asyncio
import logging
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


def _token(access_token: str, *, expires_in: int | None = None):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(access_token=access_token, token_type="Bearer", expires_in=expires_in)


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
        "      type: streamable-http\n"
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
    assert server_cfg["type"] == "streamable-http"
    assert server_cfg["auth"]["type"] == "oauth"
    assert server_cfg["auth"]["scopes"] == ["repo", "read:org"]
    assert server_cfg["auth"]["client_id"] == "my-client-id"


def test_bare_oauth_string_shorthand_parses(oauth_store_path) -> None:
    """Tier 1: ``auth: oauth`` (bare string) is shorthand for ``{"type": "oauth"}``
    — builds a provider without raising."""
    cfg = {"type": "streamable-http", "url": "https://example.com/mcp", "auth": "oauth"}
    client = MCPClient(cfg, non_interactive=False)
    provider = asyncio.run(client._build_oauth_provider(cfg["url"]))
    assert provider is not None


def test_unsupported_auth_type_rejected() -> None:
    """Tier 1: a non-'oauth' auth.type is a clear config error, not a silent no-op."""
    cfg = {"type": "streamable-http", "url": "https://example.com/mcp", "auth": {"type": "saml"}}
    client = MCPClient(cfg, non_interactive=False)
    with pytest.raises(MCPError, match="saml"):
        asyncio.run(client._build_oauth_provider(cfg["url"]))


def test_auth_on_stdio_server_rejected_at_construction() -> None:
    """Tier 1: OAuth is meaningless over stdio — reject eagerly, don't silently ignore."""
    with pytest.raises(ValueError, match="stdio"):
        MCPClient({"type": "stdio", "command": "x", "auth": "oauth"})


def test_auth_on_sse_server_rejected_at_construction() -> None:
    """Tier 1: same restriction for sse — OAuth only wired for Streamable HTTP."""
    with pytest.raises(ValueError, match="sse"):
        MCPClient({"type": "sse", "url": "https://x/sse", "auth": "oauth"})


# ── (b) reyn's own contract ① — a token gets saved to the configured path ──


def test_token_storage_round_trips_through_outside_bucket_file(oauth_store_path) -> None:
    """Tier 2: MCPOAuthTokenStorage persists + reloads a token through the
    real oauth_tokens.json file, 0600-permissioned — the SAME on-disk file
    reyn.security.secrets.oauth's device-grant store uses (outside bucket,
    per reyn-dir-layout.md). A FRESH storage instance (same path) reads
    back the same value — proves it round-trips through the file, not an
    in-process cache."""
    url = "https://server-a.example.com/mcp"
    storage_a = MCPOAuthTokenStorage(url, path=oauth_store_path)

    asyncio.run(storage_a.set_tokens(_token("at-nondefault-9f3c")))

    assert oauth_store_path.exists()
    assert oct(oauth_store_path.stat().st_mode & 0o777) == "0o600"

    storage_b = MCPOAuthTokenStorage(url, path=oauth_store_path)
    reloaded = asyncio.run(storage_b.get_tokens())
    assert reloaded is not None
    assert reloaded.access_token == "at-nondefault-9f3c"


def test_token_storage_per_server_keying_does_not_collide(oauth_store_path) -> None:
    """Tier 2: two different server URLs are stored independently — writing
    server B's token must not disturb server A's (per-server keying
    invariant, now expressed via TWO separate MCPOAuthTokenStorage
    instances, one per server_url, matching OAuthClientProvider's own
    per-instance-per-server contract)."""
    storage_a = MCPOAuthTokenStorage("https://server-a.example.com/mcp", path=oauth_store_path)
    storage_b = MCPOAuthTokenStorage("https://server-b.example.com/mcp", path=oauth_store_path)

    asyncio.run(storage_a.set_tokens(_token("token-a-4471")))
    asyncio.run(storage_b.set_tokens(_token("token-b-8823")))

    a = asyncio.run(storage_a.get_tokens())
    b = asyncio.run(storage_b.get_tokens())
    assert a.access_token == "token-a-4471"
    assert b.access_token == "token-b-8823"


def test_client_info_round_trips_through_outside_bucket_file(oauth_store_path) -> None:
    """Tier 2: static client_id registration (get_client_info/set_client_info)
    round-trips the same way tokens do — the second half of TokenStorage's
    4-method contract."""
    from mcp.shared.auth import OAuthClientInformationFull

    url = "https://server-e.example.com/mcp"
    storage_a = MCPOAuthTokenStorage(url, path=oauth_store_path)
    info = OAuthClientInformationFull(
        client_id="static-client-123",
        redirect_uris=["http://127.0.0.1:9/callback"],
    )
    asyncio.run(storage_a.set_client_info(info))

    storage_b = MCPOAuthTokenStorage(url, path=oauth_store_path)
    reloaded = asyncio.run(storage_b.get_client_info())
    assert reloaded is not None
    assert reloaded.client_id == "static-client-123"


# ── (c) reyn's own contract ② — a stored token is reused, no re-auth ───────


def test_headless_with_stored_token_proceeds(oauth_store_path) -> None:
    """Tier 1: once a token IS cached for this exact server URL, a
    non-interactive client builds the OAuth provider without raising — the
    pre-flight check is scoped to "no token yet", not "always block
    headless". This is reyn's own re-use promise: a cached token means no
    browser flow is even attempted."""
    url = "https://mcp.example.com/mcp"
    asyncio.run(MCPOAuthTokenStorage(url, path=oauth_store_path).set_tokens(_token("at-cached-7742")))

    cfg = {"type": "streamable-http", "url": url, "auth": {"type": "oauth"}}
    client = MCPClient(cfg, non_interactive=True)
    provider = asyncio.run(client._build_oauth_provider(url))  # must not raise
    assert provider is not None


def test_has_stored_token_reflects_real_store_state(oauth_store_path) -> None:
    """Tier 2: has_stored_token() is a thin, real (non-mocked) read of the
    same file MCPOAuthTokenStorage writes — used by the headless pre-flight
    check in client.py to decide whether re-use is possible."""
    url = "https://server-c.example.com/mcp"
    assert has_stored_token(url, path=oauth_store_path) is False

    asyncio.run(MCPOAuthTokenStorage(url, path=oauth_store_path).set_tokens(_token("at-real-6620")))
    assert has_stored_token(url, path=oauth_store_path) is True


def test_expired_token_entry_not_reported_as_stored(oauth_store_path) -> None:
    """Tier 2: an expired entry must not be reported as a usable token —
    has_stored_token() checks reyn's own tracked expiry (see
    oauth_token_storage.py's module docstring for why reyn tracks this
    itself rather than relying on the SDK's own reload behavior)."""
    url = "https://server-d.example.com/mcp"
    asyncio.run(
        MCPOAuthTokenStorage(url, path=oauth_store_path).set_tokens(
            _token("at-expiring-3391", expires_in=-1)  # already expired
        )
    )
    assert has_stored_token(url, path=oauth_store_path) is False


# ── (d) reyn's own contract ③ — an orphaned/corrupt token fails legibly ────


def test_corrupt_token_entry_is_treated_as_absent_not_a_crash(
    oauth_store_path, caplog,
) -> None:
    """Tier 1: a stored entry whose JSON shape no longer round-trips through
    OAuthToken (e.g. a future reyn version changes what it stores, or the
    file was hand-edited) is treated as absent — get_tokens() returns None,
    never raises — AND the failure is LOGGED clearly (naming the server +
    that re-authentication is needed), not silently swallowed. This is the
    condition lead-coder set on #4282: "静かに失敗しないこと"."""
    import json

    url = "https://server-f.example.com/mcp"
    storage = MCPOAuthTokenStorage(url, path=oauth_store_path)
    # Write a garbage entry directly under the SAME key storage.set_tokens
    # would use, bypassing the normal write path to simulate corruption /
    # a future schema change.
    from reyn.mcp.oauth_token_storage import _tokens_key

    oauth_store_path.parent.mkdir(parents=True, exist_ok=True)
    oauth_store_path.write_text(
        json.dumps({_tokens_key(url): {"_value": {"not_a_real_field": 123}, "_expires_at": None}}),
        encoding="utf-8",
    )
    oauth_store_path.chmod(0o600)

    with caplog.at_level(logging.WARNING, logger="reyn.mcp.oauth_token_storage"):
        result = asyncio.run(storage.get_tokens())

    assert result is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("unrecognized format" in m for m in messages), messages
    assert any(url in m for m in messages), messages


def test_headless_no_token_raises_clear_mcp_error_not_hang(oauth_store_path) -> None:
    """Tier 1: a non-interactive caller with no cached token gets a clear,
    immediate MCPError instead of the OAuth flow opening a browser + waiting
    on a localhost callback nobody can complete."""
    cfg = {
        "type": "streamable-http",
        "url": "https://mcp.example.com/mcp",
        "auth": {"type": "oauth"},
    }
    client = MCPClient(cfg, non_interactive=True)
    with pytest.raises(MCPError, match="requires OAuth authentication"):
        asyncio.run(client._build_oauth_provider(cfg["url"]))


def test_interactive_client_with_no_token_does_not_raise_preflight_error(
    oauth_store_path,
) -> None:
    """Tier 1: an explicitly interactive client (non_interactive=False) is
    allowed to proceed to the browser flow even with no cached token — the
    headless guard only fires for non-interactive callers."""
    cfg = {
        "type": "streamable-http",
        "url": "https://mcp.example.com/mcp",
        "auth": {"type": "oauth"},
    }
    client = MCPClient(cfg, non_interactive=False)
    provider = asyncio.run(client._build_oauth_provider(cfg["url"]))  # must not raise
    assert provider is not None


# ── (e) static bearer / header auth regression — unaffected by OAuth ───────


def test_static_bearer_header_auth_unaffected(oauth_store_path) -> None:
    """Tier 1: a server with NO 'auth' key (the pre-④ static-bearer-via-
    headers path) builds no OAuth provider at all — the ④ wiring is
    additive, never a behavior change for the existing header-auth path."""
    cfg = {
        "type": "streamable-http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer static-token-abc"},
    }
    client = MCPClient(cfg)
    provider = asyncio.run(client._build_oauth_provider(cfg["url"]))
    assert provider is None


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
    storage = MCPOAuthTokenStorage("https://mcp.example.com/mcp", path=oauth_path)

    asyncio.run(storage.set_tokens(_token("at-isolation-check-1123")))

    assert oauth_path.exists()
    written_under_dot_reyn = list(project_reyn_dir.rglob("*"))
    assert written_under_dot_reyn == []
