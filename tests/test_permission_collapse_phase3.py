"""Tier 2: #571 collapse arc Phase 3 — http.get axis + safe.http per-host gate.

Verifies the OS-invariants introduced by Phase 3:

1. `PermissionDecl` accepts `http_get` and `secret_write` axes from
   `from_dict`, with appropriate normalisation (string entries lifted
   to `{host: ...}` for http.get, secret.write accepts list[str]).
2. The compat shim expands `mcp_install: true` to include
   `http.get: [{host: registry.modelcontextprotocol.io}]` in addition
   to the Phase 2 `file.write` expansion. Idempotent; no duplicates
   when the explicit form is also declared.
3. `PermissionResolver.require_http_get` raises for undeclared hosts
   and passes for declared ones (per-host exact match, no implicit
   wildcards).
4. `PermissionResolver.require_secret_write` raises for undeclared
   keys and passes for declared ones. (No callers in Phase 3 — wired
   in Phase 5 — but the resolver method exists and is invariant-pinned.)
5. `reyn.safe.http._check_host` rejects an unauthorised host with a
   structured PermissionError that names the host and points to the
   skill.md declaration form.
6. Unset permission context (= bare-process use without harness setup)
   raises with the documented guidance.

Tier policy: real PermissionResolver instances + the real safe.http
module — no mocks.
"""
from __future__ import annotations

import pytest

from reyn.permissions.permissions import (
    PermissionDecl,
    PermissionResolver,
)

# ── PermissionDecl parsing ─────────────────────────────────────────────────────


def test_http_get_parses_dict_form():
    """Tier 2: from_dict accepts [{host: str}] verbatim."""
    decl = PermissionDecl.from_dict({
        "http.get": [{"host": "api.github.com"}, {"host": "registry.modelcontextprotocol.io"}],
    })
    hosts = [e["host"] for e in decl.http_get]
    assert hosts == ["api.github.com", "registry.modelcontextprotocol.io"]


def test_http_get_parses_bare_string_form():
    """Tier 2: from_dict normalises bare strings to {host: str}."""
    decl = PermissionDecl.from_dict({
        "http.get": ["api.github.com", "example.com"],
    })
    hosts = [e["host"] for e in decl.http_get]
    assert hosts == ["api.github.com", "example.com"]


def test_secret_write_parses_list_of_strings():
    """Tier 2: from_dict accepts list[str] for secret.write."""
    decl = PermissionDecl.from_dict({
        "secret.write": ["GITHUB_TOKEN", "STRIPE_KEY"],
    })
    assert decl.secret_write == ["GITHUB_TOKEN", "STRIPE_KEY"]


def test_secret_write_drops_non_string_entries():
    """Tier 2: from_dict drops non-string/non-int entries silently."""
    decl = PermissionDecl.from_dict({
        "secret.write": ["OK", None, {"nope": 1}, "ALSO_OK"],
    })
    assert decl.secret_write == ["OK", "ALSO_OK"]


# ── compat shim ────────────────────────────────────────────────────────────────


def test_mcp_install_compat_shim_expands_http_get():
    """Tier 2: mcp_install: true adds the registry host to http_get."""
    decl = PermissionDecl.from_dict({"mcp_install": True})
    hosts = [e["host"] for e in decl.http_get]
    assert "registry.modelcontextprotocol.io" in hosts


def test_mcp_install_compat_shim_idempotent_with_explicit_http_get():
    """Tier 2: explicit + implicit http_get entries deduplicate."""
    decl = PermissionDecl.from_dict({
        "mcp_install": True,
        "http.get": [{"host": "registry.modelcontextprotocol.io"}],
    })
    matching = [e for e in decl.http_get if e["host"] == "registry.modelcontextprotocol.io"]
    assert len(matching) == 1


def test_non_mcp_install_bool_axes_do_not_expand_http_get():
    """Tier 2: only mcp_install carries an HTTP expansion today."""
    for axis in ("mcp_drop_server", "cron_register", "index_drop"):
        decl = PermissionDecl.from_dict({axis: True})
        assert decl.http_get == [], f"{axis} should not expand to http_get"


def test_compat_shim_does_not_expand_secret_write():
    """Tier 2: no bool axis auto-expands to secret_write entries.

    Rationale: the env-var keys mcp_install will save are determined at
    runtime from the registry response, not statically by the skill
    author, so there is no useful compat-shim mapping. Skills declare
    secret.write explicitly (or it stays empty).
    """
    decl = PermissionDecl.from_dict({"mcp_install": True})
    assert decl.secret_write == []


# ── PermissionResolver gates ──────────────────────────────────────────────────


def test_require_http_get_raises_for_undeclared_host(tmp_path):
    """Tier 2: require_http_get raises with a structured error."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "api.github.com"}])
    with pytest.raises(PermissionError, match="example.com"):
        resolver.require_http_get(decl, "example.com")


def test_require_http_get_passes_for_declared_host(tmp_path):
    """Tier 2: require_http_get passes for an exact host match."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "api.github.com"}])
    resolver.require_http_get(decl, "api.github.com")


def test_require_http_get_passes_via_compat_shim(tmp_path):
    """Tier 2: end-to-end — mcp_install bool decl passes the registry host check."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl.from_dict({"mcp_install": True})
    resolver.require_http_get(decl, "registry.modelcontextprotocol.io")


def test_require_secret_write_raises_for_undeclared_key(tmp_path):
    """Tier 2: require_secret_write raises with a structured error."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(secret_write=["GITHUB_TOKEN"])
    with pytest.raises(PermissionError, match="STRIPE_KEY"):
        resolver.require_secret_write(decl, "STRIPE_KEY")


def test_require_secret_write_passes_for_declared_key(tmp_path):
    """Tier 2: require_secret_write passes for an exact key match."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(secret_write=["GITHUB_TOKEN"])
    resolver.require_secret_write(decl, "GITHUB_TOKEN")


# ── reyn.safe.http enforcement ────────────────────────────────────────────────


def test_safe_http_check_host_rejects_unauthorised():
    """Tier 2: safe.http rejects a request to an unauthorised host."""
    from reyn.safe import http as safe_http
    safe_http._set_permission_context(http_hosts=["api.github.com"])
    with pytest.raises(PermissionError, match="example.com"):
        safe_http._check_host("https://example.com/path")


def test_safe_http_check_host_accepts_authorised():
    """Tier 2: safe.http allows a request to an explicitly allowed host."""
    from reyn.safe import http as safe_http
    safe_http._set_permission_context(http_hosts=["api.github.com"])
    safe_http._check_host("https://api.github.com/repos/foo/bar")


def test_safe_http_unset_context_raises():
    """Tier 2: bare-process use without harness setup raises with guidance."""
    from reyn.safe import http as safe_http
    # Reset to uninitialised by direct assignment (= mirrors safe.file pattern).
    safe_http._context_initialised = False
    safe_http._allowed_hosts = ()
    with pytest.raises(PermissionError, match="permission context not initialised"):
        safe_http._check_host("https://example.com/")


def test_safe_http_all_verbs_gate_via_check_host(monkeypatch):
    """Tier 2: get/post/put/delete all go through _check_host."""
    from reyn.safe import http as safe_http

    safe_http._set_permission_context(http_hosts=["only.allowed.example"])
    for verb, args in (
        ("get", ("https://blocked.example/x",)),
        ("post", ("https://blocked.example/x", {"a": 1})),
        ("put", ("https://blocked.example/x", {"a": 1})),
        ("delete", ("https://blocked.example/x",)),
    ):
        with pytest.raises(PermissionError, match="blocked.example"):
            getattr(safe_http, verb)(*args)
