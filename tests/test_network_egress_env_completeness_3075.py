"""Tier 2: network-egress standard-env completeness gate (#3075).

Issue #3075's requirement is completeness, not a fix per class: EVERY
reyn-originated network egress honours the standard proxy/CA env
(``HTTP(S)_PROXY``/``NO_PROXY``, ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``), zero
exceptions. This file is the "enumerate, don't curate" spine for that claim:

* one behavioral test per egress class from #3075's enumeration table, each
  asserting the class actually reads a SENTINEL value from the standard env
  (not "the code looks right" — an observed effect of setting the env), and
* two structural (AST) guards: no ``httpx.Client(``/``httpx.AsyncClient(``
  construction in ``src/reyn`` bypasses the DRY constructor
  (``reyn._network.build_async_http_client`` / ``build_sync_http_client``), and
  the DRY constructor module DOES construct httpx clients (positive guard —
  the AST-guard pattern from ``test_present_sink_ast_guard_2708.py``: a
  bypass-check that always passes because the chokepoint is empty is not a
  guard).

A new egress class that skips this file's enumeration is invisible to the
completeness claim by construction — the intended failure mode is "someone
adds egress #7 and forgets to enumerate it here", which this file cannot
detect on its own (that's why the structural guard below matters: a NEW
call site is caught even if nobody remembers to add a class-specific test for
it, as long as it goes through ``httpx.Client``/``httpx.AsyncClient`` directly).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

SENTINEL_PROXY = "http://sentinel-proxy.invalid:9"
SENTINEL_CA = "/sentinel/ca-bundle.pem"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every standard proxy/CA var so each test controls its own sentinel."""
    from reyn._network import STANDARD_NETWORK_ENV_NAMES

    for name in STANDARD_NETWORK_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REYN_SSRF_STRICT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_CERTIFICATE", raising=False)


# ── #1 litellm ──────────────────────────────────────────────────────────────


def test_litellm_trusts_standard_proxy_env() -> None:
    """Tier 2: ensure_litellm_ready() flips litellm.aiohttp_trust_env on so the
    highest-volume egress reads HTTP(S)_PROXY like every other conforming path."""
    import litellm

    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    ensure_litellm_ready()
    assert litellm.aiohttp_trust_env is True


# ── #2 SSRF-pin (web_fetch / RegistryClient) ────────────────────────────────


def test_ssrf_pinned_client_mounts_a_transport_for_the_env_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: with a standard-env proxy set, ssrf_aware_client_kwargs (the
    public function build_async_http_client(pin_ssrf=True) delegates to)
    returns a mount for that scheme instead of silently ignoring the env
    (#3075 fix 2 — the sharpest non-conformer: explicit transport= previously
    disabled httpx's own env-proxy reading entirely)."""
    monkeypatch.setenv("HTTPS_PROXY", SENTINEL_PROXY)
    from reyn._ssrf_pin import ssrf_aware_client_kwargs

    kwargs = ssrf_aware_client_kwargs(verify=True)
    assert kwargs.get("mounts"), "expected an env-proxy mount when HTTPS_PROXY is set"


def test_ssrf_strict_env_refuses_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: REYN_SSRF_STRICT=true keeps the pin-only transport even when a
    proxy is configured (#3075 SSRF-x-proxy decision's tightening opt-in)."""
    monkeypatch.setenv("HTTPS_PROXY", SENTINEL_PROXY)
    monkeypatch.setenv("REYN_SSRF_STRICT", "true")
    from reyn._ssrf_pin import ssrf_aware_client_kwargs

    kwargs = ssrf_aware_client_kwargs(verify=True)
    assert "mounts" not in kwargs


def test_no_proxy_env_keeps_pin_only_default_transport() -> None:
    """Tier 2: with no proxy env at all, pin_ssrf=True is unchanged behavior —
    every request still goes through PinnedAsyncHTTPTransport, no mounts."""
    from reyn._ssrf_pin import PinnedAsyncHTTPTransport, ssrf_aware_client_kwargs

    kwargs = ssrf_aware_client_kwargs(verify=True)
    assert isinstance(kwargs["transport"], PinnedAsyncHTTPTransport)
    assert "mounts" not in kwargs


# ── #3 OTEL CA bridge ────────────────────────────────────────────────────────


def test_otel_bridges_standard_ca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: the standard CA env bridges to OTEL's own var (a DIFFERENT name
    than every other reyn egress reads) when reyn configures the exporter."""
    monkeypatch.setenv("SSL_CERT_FILE", SENTINEL_CA)
    from reyn.observability.otel_exporter import _bridge_standard_ca_to_otel_env

    _bridge_standard_ca_to_otel_env()
    assert os.environ.get("OTEL_EXPORTER_OTLP_CERTIFICATE") == SENTINEL_CA


def test_otel_bridge_never_overrides_explicit_operator_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: an operator who already set OTEL_EXPORTER_OTLP_CERTIFICATE
    explicitly is never silently overridden by the standard-env bridge."""
    monkeypatch.setenv("SSL_CERT_FILE", SENTINEL_CA)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "/operator/explicit.pem")
    from reyn.observability.otel_exporter import _bridge_standard_ca_to_otel_env

    _bridge_standard_ca_to_otel_env()
    assert os.environ["OTEL_EXPORTER_OTLP_CERTIFICATE"] == "/operator/explicit.pem"


# ── #4 ddgs / web_search ─────────────────────────────────────────────────────


def test_ddgs_backend_resolves_standard_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: resolve_env_proxy_url (fed to DDGS(proxy=...)) reads the standard
    HTTPS_PROXY, not ddgs' own non-standard DDGS_PROXY."""
    monkeypatch.setenv("HTTPS_PROXY", SENTINEL_PROXY)
    from reyn._network import resolve_env_proxy_url

    assert resolve_env_proxy_url("https") == SENTINEL_PROXY


def test_ddgs_backend_honours_no_proxy_absence() -> None:
    """Tier 2: with no proxy env set, resolve_env_proxy_url returns None (ddgs
    then uses its own default / DDGS_PROXY fallback, unaffected)."""
    from reyn._network import resolve_env_proxy_url

    assert resolve_env_proxy_url("https") is None


# ── #5 subprocess (sandbox child env) ───────────────────────────────────────


def test_sandbox_child_env_includes_standard_set_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the sandbox forwards the standard proxy/CA env to EVERY spawned
    child by default (#3075 fix 5) — even when the caller's own
    ``env_passthrough`` allowlist is empty (the git-clone-only forwarding this
    generalises always listed its vars explicitly)."""
    monkeypatch.setenv("HTTP_PROXY", SENTINEL_PROXY)
    monkeypatch.setenv("SSL_CERT_FILE", SENTINEL_CA)
    from reyn.security.sandbox.policy import SandboxPolicy, resolve_passthrough_env

    policy = SandboxPolicy(env_passthrough=[])
    env = resolve_passthrough_env(policy)
    assert env.get("HTTP_PROXY") == SENTINEL_PROXY
    assert env.get("SSL_CERT_FILE") == SENTINEL_CA


def test_sandbox_child_env_still_honours_explicit_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: additive, not a replacement — an operator-declared
    env_passthrough entry is still forwarded alongside the standard set."""
    monkeypatch.setenv("MY_CUSTOM_VAR", "keep-me")
    from reyn.security.sandbox.policy import SandboxPolicy, resolve_passthrough_env

    policy = SandboxPolicy(env_passthrough=["MY_CUSTOM_VAR"])
    env = resolve_passthrough_env(policy)
    assert env.get("MY_CUSTOM_VAR") == "keep-me"


def test_noop_backend_and_seatbelt_and_landlock_share_the_chokepoint() -> None:
    """Tier 2: structural — all three sandbox backends call
    resolve_passthrough_env (not a hand-duplicated env_passthrough loop each),
    so #3075's default-forward can't drift out of sync between them."""
    import reyn.security.sandbox.backends.landlock as landlock_mod
    import reyn.security.sandbox.backends.seatbelt as seatbelt_mod
    import reyn.security.sandbox.noop_backend as noop_mod

    for mod in (noop_mod, seatbelt_mod, landlock_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "resolve_passthrough_env(policy)" in src, (
            f"{mod.__name__} does not call the shared resolve_passthrough_env "
            "chokepoint — #3075's default proxy/CA forwarding can silently "
            "drift out of sync with the other backends"
        )


# ── #6 ssl_verify:false audit-event ─────────────────────────────────────────


def test_verify_false_emits_audit_event_once() -> None:
    """Tier 2: a verify=False client construction emits network_ssl_verify_disabled
    exactly once per (process, egress) — never silent (#3075 ssl_verify decision)."""
    from reyn._network import (
        build_sync_http_client,
        reset_ssl_verify_disabled_latch_for_tests,
    )
    from reyn.core.events.events import EventLog

    reset_ssl_verify_disabled_latch_for_tests()
    events = EventLog()
    c1 = build_sync_http_client(verify=False, events=events, egress="test_once")
    c1.close()
    after_first = [e for e in events.all() if e.type == "network_ssl_verify_disabled"]
    assert after_first, "expected one network_ssl_verify_disabled event after the first verify=False construction"
    assert after_first[0].data.get("egress") == "test_once"

    c2 = build_sync_http_client(verify=False, events=events, egress="test_once")
    c2.close()
    after_second = [e for e in events.all() if e.type == "network_ssl_verify_disabled"]
    # Latched: the second verify=False construction for the SAME egress must not
    # add a second event — the audit trail stays exactly what it was.
    assert after_second == after_first
    reset_ssl_verify_disabled_latch_for_tests()


def test_verify_true_never_emits_the_audit_event() -> None:
    """Tier 2: negative guard — verify=True (the default / recommended path)
    never emits network_ssl_verify_disabled."""
    from reyn._network import (
        build_sync_http_client,
        reset_ssl_verify_disabled_latch_for_tests,
    )
    from reyn.core.events.events import EventLog

    reset_ssl_verify_disabled_latch_for_tests()
    events = EventLog()
    c = build_sync_http_client(verify=True, events=events, egress="test_true")
    c.close()
    assert not [e for e in events.all() if e.type == "network_ssl_verify_disabled"]


# ── structural gate: no raw httpx.Client/AsyncClient bypass ────────────────

_CONSTRUCTOR_MODULE = "src/reyn/_network.py"
# Additional intentional construction sites: the SSRF-pin transport itself
# wraps httpx.AsyncHTTPTransport (a *transport*, not a Client — out of this
# gate's scope) and constructs the plain AsyncHTTPTransport used for the
# proxy-mount path; that file is the transport-level primitive the DRY
# constructor builds on, not a bypass of it.
_ALLOWED_TRANSPORT_MODULE = "src/reyn/_ssrf_pin.py"


def _is_httpx_client_construction(node: ast.AST) -> str | None:
    """Return 'Client' / 'AsyncClient' if *node* constructs one, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    # httpx.Client(...) / httpx.AsyncClient(...)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "httpx" and func.attr in ("Client", "AsyncClient"):
            return func.attr
    # Client(...) / AsyncClient(...) after `from httpx import Client`
    if isinstance(func, ast.Name) and func.id in ("Client", "AsyncClient"):
        return func.id
    return None


def test_no_raw_httpx_client_construction_bypasses_the_dry_constructor() -> None:
    """Tier 2: structural — every httpx.Client(...)/httpx.AsyncClient(...)
    construction in src/reyn lives inside the DRY constructor module
    (reyn._network). A new call site that free-hands its own client
    construction re-opens the #3075 "almost every egress" gap and must fail
    here instead of shipping silently non-conformant."""
    root = _repo_root()
    src = root / "src" / "reyn"
    constructor_path = (root / _CONSTRUCTOR_MODULE).resolve()
    transport_path = (root / _ALLOWED_TRANSPORT_MODULE).resolve()

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        resolved = py.resolve()
        if resolved in (constructor_path, transport_path):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            kind = _is_httpx_client_construction(node)
            if kind:
                offenders.append(f"{py.relative_to(root)}:{node.lineno} ({kind})")

    assert not offenders, (
        "Raw httpx.Client(...)/httpx.AsyncClient(...) construction outside the "
        "DRY constructor (reyn._network.build_async_http_client / "
        "build_sync_http_client) — bypasses #3075's proxy/CA + SSRF-pin env "
        f"wiring. Offending sites: {offenders}"
    )


def test_dry_constructor_module_actually_constructs_httpx_clients() -> None:
    """Tier 2: positive guard — the constructor module DOES construct
    httpx.Client/AsyncClient (the chokepoint exists and is used, not merely
    an empty pass-through the bypass-check above vacuously satisfies)."""
    root = _repo_root()
    constructor_path = root / _CONSTRUCTOR_MODULE
    tree = ast.parse(constructor_path.read_text(encoding="utf-8"))
    kinds = {
        kind
        for node in ast.walk(tree)
        if (kind := _is_httpx_client_construction(node)) is not None
    }
    assert kinds == {"Client", "AsyncClient"}, (
        f"expected reyn._network to construct both httpx.Client and "
        f"httpx.AsyncClient; found {kinds}"
    )
