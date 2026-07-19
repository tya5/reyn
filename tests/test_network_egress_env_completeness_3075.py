"""Tier 2: network-egress standard-env completeness gate (#3075).

Issue #3075's requirement is completeness, not a fix per class: EVERY
reyn-originated network egress honours the standard proxy/CA env
(``HTTP(S)_PROXY``/``NO_PROXY``, ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``), zero
exceptions. This file is the "enumerate, don't curate" spine for that claim.

The enumeration was grep-derived, not memory-derived (an earlier curated version
missed two whole transport families — ``urllib.request`` and third-party libs —
which two reviewers found separately, the classic "curated subset" tell). The
egress classes, by transport:

* **httpx** (async/sync) — litellm, web_fetch/RegistryClient (SSRF-pin),
  remote-MCP/OAuth/webhook/remote-repl. All go through the DRY
  ``reyn._network.build_async_http_client`` / ``build_sync_http_client``.
* **urllib.request** — safe-mode HTTP (``reyn.api.safe.http``) + safe-mode MCP
  registry (``reyn.mcp.registry``). Both go through the DRY
  ``reyn._ssrf_pin.ssrf_aware_urllib_opener``.
* **third-party libs reyn does not own the client for** — ddgs (web_search),
  huggingface_hub (faster-whisper transcription model download, via
  ``requests``), fastmcp
  (remote-MCP transport, via ``httpx``). These conform by the lib's own
  ``trust_env``-style default; the witness tests below RED if a lib ships a
  transport that stops honouring the env (the exact litellm-``aiohttp_trust_env``
  degradation, generalised).
* **subprocess** — uvx/npx/uv, via the sandbox child-env forward.

For each class: a behavioral test asserting a SENTINEL value from the standard
env is actually honoured (an observed effect, not "the code looks right"), plus
structural (AST) guards that no construction in ``src/reyn`` bypasses the DRY
constructor for that transport — one for ``httpx.Client``/``AsyncClient``, one
for ``urllib.request.build_opener`` — mirroring
``test_present_sink_ast_guard_2708.py`` (a bypass-check that vacuously passes
because the chokepoint is empty is not a guard, so each has a positive twin).

A NEW egress that free-hands its own client/opener is caught by the structural
guard even if nobody remembers to add a class-specific behavioral test for it —
that is what keeps "all" from decaying to "almost all" as the code grows.
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
    monkeypatch.delenv("SSL_VERIFY", raising=False)
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)


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


# ── urllib.request egress (safe.http + mcp.registry) ────────────────────────


def _proxy_handler_proxies(opener: object) -> dict:
    """Extract the active ProxyHandler's proxy map from a urllib opener (public
    surface: OpenerDirector.handlers is a documented attribute)."""
    for handler in opener.handlers:  # type: ignore[attr-defined]
        if type(handler).__name__ == "ProxyHandler":
            return dict(getattr(handler, "proxies", {}))
    return {}


def test_urllib_opener_routes_through_proxy_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the urllib egress (safe.http / mcp.registry) honours the standard
    proxy env — with HTTPS_PROXY set, the opener carries an active ProxyHandler
    for that scheme instead of the previous env-blind pinned-only opener."""
    monkeypatch.setenv("HTTPS_PROXY", "http://8.8.8.8:3128")  # public proxy IP
    from reyn._ssrf_pin import ssrf_aware_urllib_opener

    opener = ssrf_aware_urllib_opener()
    assert _proxy_handler_proxies(opener).get("https") == "http://8.8.8.8:3128"


def test_urllib_opener_pins_when_no_proxy() -> None:
    """Tier 2: with no proxy env, the urllib opener keeps the DNS-rebind-resistant
    pinned handlers and no active proxy (unchanged pre-#3075 security posture)."""
    from reyn._ssrf_pin import ssrf_aware_urllib_opener

    opener = ssrf_aware_urllib_opener()
    handler_names = [type(h).__name__ for h in opener.handlers]  # type: ignore[attr-defined]
    assert any("Pinned" in n for n in handler_names)
    assert _proxy_handler_proxies(opener) == {}


def test_urllib_opener_strict_refuses_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: REYN_SSRF_STRICT=true makes the urllib egress refuse the env proxy
    and keep its own pin — the build_opener auto-added default ProxyHandler must
    NOT silently re-enable proxying under strict."""
    monkeypatch.setenv("HTTPS_PROXY", "http://8.8.8.8:3128")
    monkeypatch.setenv("REYN_SSRF_STRICT", "true")
    from reyn._ssrf_pin import ssrf_aware_urllib_opener

    opener = ssrf_aware_urllib_opener()
    assert _proxy_handler_proxies(opener) == {}
    handler_names = [type(h).__name__ for h in opener.handlers]  # type: ignore[attr-defined]
    assert any("Pinned" in n for n in handler_names)


def test_urllib_opener_private_ip_proxy_exempt_but_metadata_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: an operator proxy on an RFC1918 address is exempt from the
    private-IP SSRF block (#3075 architect decision), but a proxy pointed at the
    cloud-metadata endpoint is STILL a hard deny (never a legit operator proxy)."""
    from reyn._ssrf_guard import SSRFBlocked
    from reyn._ssrf_pin import ssrf_aware_urllib_opener

    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.5:3128")  # RFC1918 — exempt
    ssrf_aware_urllib_opener()  # no raise

    monkeypatch.setenv("HTTPS_PROXY", "http://169.254.169.254:3128")  # metadata
    with pytest.raises(SSRFBlocked):
        ssrf_aware_urllib_opener()


def test_urllib_egress_resolves_standard_ca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: the urllib egress's CA resolution honours the standard CA env,
    including REQUESTS_CA_BUNDLE (which the ssl module does NOT read natively —
    the gap #3075 closes for this class). Uses a real file so the existing-path
    check in the resolver passes."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as fh:
        fh.write(b"# not a real cert, just an existing path for the resolver\n")
        ca_path = fh.name
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", ca_path)
    from reyn._network import resolve_ssl_verify_from_env

    assert resolve_ssl_verify_from_env() == ca_path


def test_urllib_ssl_verify_false_emits_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: SSL_VERIFY=false on the urllib egress emits the same
    network_ssl_verify_disabled P6 audit-event (never silent), wired through the
    opener's CA-context builder."""
    monkeypatch.setenv("SSL_VERIFY", "false")
    from reyn._network import reset_ssl_verify_disabled_latch_for_tests
    from reyn._ssrf_pin import _build_ca_ssl_context
    from reyn.core.events.events import EventLog

    reset_ssl_verify_disabled_latch_for_tests()
    events = EventLog()
    _build_ca_ssl_context(events=events, egress="urllib_test")
    matches = [e for e in events.all() if e.type == "network_ssl_verify_disabled"]
    assert matches
    assert matches[0].data.get("egress") == "urllib_test"
    reset_ssl_verify_disabled_latch_for_tests()


# ── third-party libs reyn doesn't own the client for (degradation witnesses) ──


def test_ddgs_backend_uses_standard_env_proxy_resolver() -> None:
    """Tier 2: structural — the ddgs backend feeds DDGS(proxy=...) from the
    standard-env resolver (not ddgs' own non-standard DDGS_PROXY). A refactor
    that drops the proxy= wiring reopens the env-blind gap."""
    import inspect

    from reyn.tools.search_backends import duckduckgo

    src = inspect.getsource(duckduckgo)
    assert "resolve_env_proxy_url" in src
    assert "DDGS(proxy=" in src


def test_huggingface_hub_session_trusts_env() -> None:
    """Tier 2: degradation witness — huggingface_hub (faster-whisper transcription
    model download) uses a
    requests Session with trust_env=True, so it honours HTTP(S)_PROXY /
    REQUESTS_CA_BUNDLE. RED if a future HF release ships trust_env=False (the
    litellm-aiohttp_trust_env degradation, in another lib)."""
    pytest.importorskip("huggingface_hub")
    from huggingface_hub.utils import get_session

    session = get_session()
    assert session.trust_env is True


def test_fastmcp_remote_transport_built_on_env_trusting_httpx() -> None:
    """Tier 2: degradation witness — fastmcp's remote-MCP transport
    (StreamableHttpTransport, which reyn passes to fastmcp.Client without owning
    the httpx client) is built on httpx and does NOT disable trust_env, so it
    honours the standard proxy/CA env by httpx's default. RED if fastmcp swaps to
    a non-env transport or sets trust_env=False."""
    import inspect

    pytest.importorskip("fastmcp")
    from fastmcp.client.transports import StreamableHttpTransport

    src = inspect.getsource(StreamableHttpTransport)
    assert "httpx" in src
    assert "trust_env=False" not in src


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


# ── structural gate: no raw urllib build_opener bypass ─────────────────────

# The urllib DRY constructor is ssrf_aware_urllib_opener in _ssrf_pin.py, so the
# ONLY build_opener call in src/reyn allowed to exist is the one inside it.
_URLLIB_CONSTRUCTOR_MODULE = "src/reyn/_ssrf_pin.py"


def _is_build_opener_call(node: ast.AST) -> bool:
    """True for a ``urllib.request.build_opener(...)`` / ``build_opener(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "build_opener":
        return True
    if isinstance(func, ast.Name) and func.id == "build_opener":
        return True
    return False


def test_no_raw_urllib_build_opener_bypasses_the_dry_constructor() -> None:
    """Tier 2: structural — every urllib.request.build_opener(...) in src/reyn
    lives inside the DRY urllib constructor (ssrf_aware_urllib_opener in
    reyn._ssrf_pin). This is the guard whose ABSENCE let the urllib egress
    (safe.http + mcp.registry) ship env-blind before #3075: the httpx-only AST
    guard could not see a urllib.request bypass. A new urllib egress that
    free-hands its own build_opener now fails here, named file:line."""
    root = _repo_root()
    src = root / "src" / "reyn"
    constructor_path = (root / _URLLIB_CONSTRUCTOR_MODULE).resolve()

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        if py.resolve() == constructor_path:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _is_build_opener_call(node):
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "Raw urllib.request.build_opener(...) construction outside the DRY urllib "
        "constructor (reyn._ssrf_pin.ssrf_aware_urllib_opener) — bypasses #3075's "
        "standard proxy/CA env + REYN_SSRF_STRICT wiring for the urllib egress. "
        f"Route it through ssrf_aware_urllib_opener instead. Offending sites: {offenders}"
    )


def test_urllib_dry_constructor_module_actually_calls_build_opener() -> None:
    """Tier 2: positive twin — the urllib constructor module DOES call
    build_opener (the chokepoint exists and is used, so the bypass-check above
    is not vacuously green on an empty chokepoint)."""
    root = _repo_root()
    constructor_path = root / _URLLIB_CONSTRUCTOR_MODULE
    tree = ast.parse(constructor_path.read_text(encoding="utf-8"))
    calls = [node.lineno for node in ast.walk(tree) if _is_build_opener_call(node)]
    assert calls, (
        "reyn._ssrf_pin.ssrf_aware_urllib_opener must call urllib.request."
        "build_opener (the single allowed urllib-opener chokepoint) — none found"
    )
