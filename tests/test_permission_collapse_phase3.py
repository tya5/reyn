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

import asyncio

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


def test_legacy_bool_keys_do_not_expand_to_http_get_or_secret_write():
    """Tier 2: post-Phase-5, legacy bool keys parse as no-ops.

    Phase 3 introduced a compat-shim expansion (`mcp_install: true` →
    http.get [registry host]) so existing skills kept working. Phase 5
    removed both the bool axis AND the shim. Legacy keys parse with a
    DeprecationWarning but contribute nothing to the decl.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for axis in ("mcp_install", "mcp_drop_server", "cron_register", "index_drop"):
            decl = PermissionDecl.from_dict({axis: True})
            assert decl.http_get == [], f"{axis} must not expand to http_get post-Phase-5"
            assert decl.secret_write == [], f"{axis} must not expand to secret_write"


# ── PermissionResolver gates ──────────────────────────────────────────────────


def test_require_http_get_raises_for_undeclared_host(tmp_path):
    """Tier 2: require_http_get raises with DeprecationWarning fallback for undeclared hosts.

    #571 Phase 7: with no http.get declaration at all, the resolver
    emits a DeprecationWarning and falls back to the legacy
    ``web.fetch`` prompt. Without a bus, it raises with a clear
    "not declared" message.
    """
    import warnings
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "api.github.com"}])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(PermissionError, match="example.com"):
            asyncio.run(resolver.require_http_get(decl, "example.com"))


def test_require_http_get_passes_for_declared_host(tmp_path):
    """Tier 2: require_http_get passes silently for a persisted approval.

    #571 Phase 7: specific declarations expect to be approved at
    startup_guard time. This test simulates that — session-approve
    the host as the resolver would after startup_guard's prompt,
    then verify runtime require_http_get passes silently.
    """
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "api.github.com"}])
    resolver._session["test_skill/http.get/api.github.com"] = True
    asyncio.run(resolver.require_http_get(decl, "api.github.com", skill_name="test_skill"))


def test_require_http_get_passes_via_explicit_decl(tmp_path):
    """Tier 2: explicit http.get declaration with persisted approval passes silently."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl.from_dict({
        "http.get": [{"host": "registry.modelcontextprotocol.io"}],
    })
    resolver._session["mcp_install/http.get/registry.modelcontextprotocol.io"] = True
    asyncio.run(resolver.require_http_get(
        decl, "registry.modelcontextprotocol.io", skill_name="mcp_install",
    ))


def test_require_http_get_wildcard_prompts_via_bus(tmp_path):
    """Tier 2: wildcard http.get fires per-host 4-layer prompt via bus.

    #571 Phase 7: ``http.get: ["*"]`` means "ask the operator at
    runtime per host". The 4-layer flow uses ``_approve`` against
    the ``<skill>/http.get/<host>`` key.
    """
    from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

    class _AlwaysBus(InterventionBus):
        def __init__(self) -> None:
            self.requests: list[UserIntervention] = []
        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            self.requests.append(iv)
            return InterventionAnswer(choice_id="always")

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "*"}])
    bus = _AlwaysBus()
    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    assert len(bus.requests) == 1
    assert "example.com" in bus.requests[0].prompt


def test_require_http_get_wildcard_persists_after_always(tmp_path):
    """Tier 2: ALWAYS choice persists, so subsequent same-host calls pass silently."""
    from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

    class _AlwaysBus(InterventionBus):
        def __init__(self) -> None:
            self.requests: list[UserIntervention] = []
        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            self.requests.append(iv)
            return InterventionAnswer(choice_id="always")

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "*"}])
    bus = _AlwaysBus()
    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    # Second call should not have prompted again — total still 1.
    assert len(bus.requests) == 1


def test_require_http_get_wildcard_without_bus_raises(tmp_path):
    """Tier 2: wildcard without bus raises (= sync subprocess can't prompt)."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "*"}])
    with pytest.raises(PermissionError, match="interactive prompt"):
        asyncio.run(resolver.require_http_get(decl, "example.com", None, "test_skill"))


def test_require_http_get_no_decl_emits_deprecation_warning(tmp_path):
    """Tier 2: no http.get declaration → DeprecationWarning + legacy compat path."""
    import warnings

    from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

    class _AlwaysBus(InterventionBus):
        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            return InterventionAnswer(choice_id="always")

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl()  # no http.get declared at all
    bus = _AlwaysBus()

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    deprecation_warnings = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert len(deprecation_warnings) == 1
    assert "http.get" in str(deprecation_warnings[0].message)


def test_require_http_get_legacy_web_fetch_allow_pre_approves(tmp_path):
    """Tier 2: legacy ``web.fetch: allow`` config short-circuits all hosts."""
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
    )
    decl = PermissionDecl(http_get=[{"host": "*"}])  # wildcard
    # Should pass without prompting because legacy web.fetch: allow wins.
    asyncio.run(resolver.require_http_get(decl, "example.com", None, "test_skill"))


def test_require_http_get_config_deny_overrides_wildcard(tmp_path):
    """Tier 2: ``web.fetch: deny`` config raises regardless of wildcard decl."""
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "deny"},
        project_root=tmp_path,
    )
    decl = PermissionDecl(http_get=[{"host": "*"}])
    with pytest.raises(PermissionError, match="deny"):
        asyncio.run(resolver.require_http_get(decl, "example.com", None, "test_skill"))


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


def test_require_secret_write_wildcard_passes_any_key(tmp_path):
    """Tier 2: secret.write: ['*'] wildcard authorises any key.

    #571 Phase 6: covers the mcp_install case where the env-var key
    set is determined at runtime from the registry response. The
    actual security gate is the operator's per-value prompt at
    op-execution time; the wildcard is the author's acknowledgement.
    """
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(secret_write=["*"])
    resolver.require_secret_write(decl, "UNFORESEEN_KEY")
    resolver.require_secret_write(decl, "ANOTHER_KEY")
    resolver.require_secret_write(decl, "PG_PASSWORD")


def test_require_secret_write_wildcard_alongside_specific_key(tmp_path):
    """Tier 2: wildcard alongside specific keys still authorises any key."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(secret_write=["GITHUB_TOKEN", "*"])
    resolver.require_secret_write(decl, "GITHUB_TOKEN")
    resolver.require_secret_write(decl, "OTHER_KEY")


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
