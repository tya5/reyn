"""DRY constructor for every reyn-originated ``httpx`` client (#3075).

**Not a network layer.** reyn does not reinvent proxy/CA configuration — the
standard env (``HTTP(S)_PROXY``/``NO_PROXY``, ``SSL_CERT_FILE``/
``REQUESTS_CA_BUNDLE``) is the single source of truth, and Python's own libs
(``httpx``, ``litellm``, the sandbox backends) already read it when given the
chance. This module is the one small **constructor** every reyn-owned httpx
client goes through, so "does this client honour the standard env" is answered
once, here, instead of re-derived ad hoc at each of the ~7 construction sites
that existed before #3075 (some honouring it by accident of never passing
``transport=``, one (SSRF-pin) not honouring it at all — see issue #3075's
enumeration table).

Two constructors:

* :func:`build_async_http_client` — the common case (all current production
  sites are async).
* :func:`build_sync_http_client` — sync sibling for the one sync call site
  (``reyn.dev.dogfood.publish``).

Both default to plain ``httpx`` behaviour (``trust_env=True`` is httpx's own
default and is preserved as long as no ``transport=`` is passed — see
``httpx.Client.__init__``: ``allow_env_proxies = trust_env and transport is
None``). ``pin_ssrf=True`` opts a client into
:func:`reyn._ssrf_pin.ssrf_aware_client_kwargs`, the DNS-rebind-resistant,
proxy-aware transport used for LLM-reachable fetch surfaces (``web_fetch``,
the MCP ``RegistryClient``) — this is the only case that needs a ``transport=``
override, and centralizing it here is what keeps proxy-awareness attached to
every future SSRF-pinned client automatically.

The structural completeness gate (``tests/network/test_egress_env_completeness.py``)
asserts no ``httpx.Client(``/``httpx.AsyncClient(`` construction in ``src/reyn``
bypasses these two functions — so a new call site that free-hands its own
``httpx.AsyncClient(...)`` fails CI instead of silently re-opening the "almost
every egress" gap.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from reyn.core.events.events import EventLog

logger = logging.getLogger(__name__)

# ── standard env enumeration (#3075 completeness gate reads these) ────────────

STANDARD_PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
STANDARD_CA_ENV_NAMES: tuple[str, ...] = (
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "PIP_CERT",
)
# The full set the sandbox forwards to every child by default (#3075 fix 5) —
# a curated, known-non-secret allowlist, not a relaxation of the sandboxed
# child's clean-env floor (secrets stay denied).
STANDARD_NETWORK_ENV_NAMES: tuple[str, ...] = (
    STANDARD_PROXY_ENV_NAMES + STANDARD_CA_ENV_NAMES
)

# ── ssl-verify-disabled audit hook (#3075 "ssl_verify:false" decision) ────────
# One-time WARN + one P6 audit-event per (process, egress name) — never silent,
# but never spammy either. A module-level latch (not per-client-instance) is
# deliberate: the operator-set env is process-wide, so "affected egress" is a
# process-wide fact reported once per egress class, mirroring the litellm
# import-log-routing latch pattern in ``llm/litellm_bootstrap.py``.
_ssl_verify_disabled_latched: set[str] = set()


def note_ssl_verify_disabled(
    events: "EventLog | None", egress: str
) -> None:
    """WARN once + emit ``network_ssl_verify_disabled`` (P6) the first time a
    given *egress* class resolves ``verify=False`` in this process.

    Called by every constructor call site whose resolved ``verify`` value is
    ``False`` — the recommended path is a custom CA (``SSL_CERT_FILE``);
    ``SSL_VERIFY=false`` is an explicit, audited escape hatch, not a silent one.
    """
    if egress in _ssl_verify_disabled_latched:
        return
    _ssl_verify_disabled_latched.add(egress)
    logger.warning(
        "SSL certificate verification is DISABLED for %s egress (SSL_VERIFY=false "
        "or an equivalent config override). This is an explicit, audited escape "
        "hatch — prefer a custom CA bundle via SSL_CERT_FILE/REQUESTS_CA_BUNDLE.",
        egress,
    )
    if events is not None:
        events.emit("network_ssl_verify_disabled", egress=egress)


def reset_ssl_verify_disabled_latch_for_tests() -> None:
    """Test-only: clear the one-shot latch so a test can re-observe the WARN/event."""
    _ssl_verify_disabled_latched.clear()


def resolve_env_proxy_url(scheme: str = "https") -> str | None:
    """Return the standard-env proxy URL for *scheme*, or ``None``.

    For libraries that take a single proxy URL string rather than reading env
    themselves (``ddgs.DDGS(proxy=...)`` — #3075 fix 4). Honours the same
    upper/lowercase + scheme-specific-then-ALL_PROXY priority httpx's own
    ``get_environment_proxies`` uses, without pulling in NO_PROXY parsing (the
    caller here is a single outbound search call, not a per-request mount
    table).
    """
    names = (f"{scheme.upper()}_PROXY", f"{scheme.lower()}_proxy", "ALL_PROXY", "all_proxy")
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


# ── DRY httpx client constructors ──────────────────────────────────────────────


def _resolve_verify_and_note(
    verify: "bool | str", *, events: "EventLog | None", egress: str
) -> "bool | str":
    if verify is False:
        note_ssl_verify_disabled(events, egress)
    return verify


def build_async_http_client(
    *,
    verify: "bool | str" = True,
    pin_ssrf: bool = False,
    events: "EventLog | None" = None,
    egress: str = "unknown",
    **client_kwargs: Any,
) -> "httpx.AsyncClient":
    """The one constructor for every reyn-owned ``httpx.AsyncClient`` (#3075).

    ``pin_ssrf=True`` routes through :func:`reyn._ssrf_pin.ssrf_aware_client_kwargs`
    (DNS-rebind pin, proxy-aware) — use for any client that fetches an
    LLM-supplied or otherwise untrusted URL (``web_fetch``, the MCP
    ``RegistryClient``). ``pin_ssrf=False`` (default) builds a plain
    ``httpx.AsyncClient(verify=verify, **client_kwargs)`` — httpx's own
    ``trust_env=True`` default (unset here, so it stays the default) already
    honours the standard proxy env because no ``transport=`` is passed.

    ``events``/``egress`` feed the ``verify=False`` audit hook (best-effort —
    pass ``events=None`` for construction sites with no ``EventLog`` in scope;
    the WARN still fires, only the P6 audit-event is skipped).
    """
    import httpx

    verify = _resolve_verify_and_note(verify, events=events, egress=egress)
    if pin_ssrf:
        from reyn._ssrf_pin import ssrf_aware_client_kwargs

        transport_kwargs = ssrf_aware_client_kwargs(verify=verify)
        return httpx.AsyncClient(**transport_kwargs, **client_kwargs)
    return httpx.AsyncClient(verify=verify, **client_kwargs)


def build_sync_http_client(
    *,
    verify: "bool | str" = True,
    events: "EventLog | None" = None,
    egress: str = "unknown",
    **client_kwargs: Any,
) -> "httpx.Client":
    """Sync sibling of :func:`build_async_http_client`.

    No ``pin_ssrf`` option — no current sync call site fetches an untrusted URL
    (the SSRF-pin transport is async-only, wrapping ``httpx.AsyncHTTPTransport``);
    add one here if a sync SSRF-pinned client is ever needed.
    """
    import httpx

    verify = _resolve_verify_and_note(verify, events=events, egress=egress)
    return httpx.Client(verify=verify, **client_kwargs)
