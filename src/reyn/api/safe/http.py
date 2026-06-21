"""Safe-mode HTTP for stdlib skills.

Exposes the same ``get`` / ``post`` / ``put`` / ``delete`` surface as
``reyn.api.unsafe.http`` (= ``urllib.request``-backed, no extra deps),
but in the ``reyn.api.safe.*`` namespace so safe-mode python steps can
import it through the AST allowlist.

Permission model (#571 Phase 3)
-------------------------------

Each call is gated against the calling skill's declared host allowlist
(``permissions.http.get: [{host: "..."}]``). The host is parsed from
the URL and checked at every method entry; an unauthorised host raises
``PermissionError`` and the safe-mode step fails with a structured
error. Bool axes that cover an HTTP host (= ``mcp_install: true`` for
the MCP registry) auto-expand to the equivalent ``http.get`` entry via
the ``PermissionDecl.from_dict`` compat shim, so existing bool-decl
skills keep working without an explicit ``http.get`` declaration.

Mirrors :mod:`reyn.api.safe.file`'s permission-context contract: the
parent process configures the allowlist via :func:`_set_permission_context`
before the user step runs; the python harness wires that.

Internal layering
-----------------

This module is reyn-package internal code (= not subject to the
safe-mode AST validator). It freely imports ``urllib.request`` /
``urllib.error``; the validator only rejects user-code imports outside
the allowlist, and ``reyn.api.safe.*`` is admitted.

Return envelope
---------------

All four methods return ``{status: int, body: str, headers: dict}``.
HTTP error statuses (4xx / 5xx) are NOT raised — callers inspect
``status``. Mirrors ``reyn.api.unsafe.http`` exactly.
"""
from __future__ import annotations

import json as _json
from typing import Any
from urllib.error import HTTPError as _HTTPError
from urllib.parse import urlparse as _urlparse
from urllib.request import HTTPRedirectHandler as _HTTPRedirectHandler
from urllib.request import Request as _Request
from urllib.request import build_opener as _build_opener

from reyn import _ssrf_guard
from reyn._http_limits import read_capped

# ── Internal state ─────────────────────────────────────────────────────────
#
# Set once at python harness start-up via :func:`_set_permission_context`.
# Used by :func:`_check_host` to gate every outbound call. Mirrors
# ``reyn.api.safe.file``'s module-globals contract.

_allowed_hosts: tuple[str, ...] = ()
_context_initialised: bool = False


def _set_permission_context(
    *,
    http_hosts: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Wire the host allowlist into this module.

    Called by :mod:`reyn.core.kernel._python_harness` before the user step
    runs. Tests that exercise the http API directly may call this to
    establish a controlled context; production code should not.

    Idempotent — calling this overwrites the previous context. Passing
    ``None`` or an empty list leaves the allowlist empty, meaning every
    HTTP call is rejected.
    """
    global _allowed_hosts, _context_initialised
    _allowed_hosts = tuple(http_hosts or ())
    _context_initialised = True


def _check_host(url: str) -> None:
    """Raise PermissionError if ``url``'s host is disallowed.

    L1: the declared ``http_hosts`` allowlist. L2 (#1956 SSRF): the host's
    resolved IP must not be link-local / metadata / loopback / private (private
    allowed only via the ``web.fetch.allow_private_ips`` operator opt-in). L2
    runs on the allowlisted host too — an allowlisted host can still resolve to,
    or (via a redirect hop) BE, an internal IP. ``SSRFBlocked`` is a
    ``PermissionError`` subclass, so callers catching ``PermissionError`` handle
    both layers.
    """
    if not _context_initialised:
        raise PermissionError(
            "reyn.api.safe.http: permission context not initialised. This "
            "module must be invoked from a PythonRunner-managed safe-mode "
            "step; bare-process use requires calling "
            "_set_permission_context(http_hosts=[...]) first "
            f"(request attempted: {url!r})."
        )
    parsed = _urlparse(url)
    host = parsed.hostname or ""
    if host in _allowed_hosts:
        # L2 (#1956 SSRF): IP-deny even for an allowlisted host.
        _ssrf_guard.assert_fetch_host_allowed(
            host, allow_private=_ssrf_guard.resolve_allow_private()
        )
        return
    raise PermissionError(
        f"reyn.api.safe.http: request to host {host!r} (url={url!r}) is not "
        f"in the declared http_hosts {list(_allowed_hosts)}. Declare it "
        f"in skill.md frontmatter:\n"
        f"  permissions:\n"
        f"    http.get:\n"
        f"      - host: {host}\n"
    )


class _SSRFSafeRedirectHandler(_HTTPRedirectHandler):
    """#1956: re-validate the host on EVERY redirect hop (L1 allowlist + L2 SSRF
    IP-deny). Stock urllib follows redirects to ANY host with no re-check, so an
    allowlisted host could redirect to a link-local / metadata / loopback /
    private target. ``_check_host`` raises (PermissionError / SSRFBlocked) on a
    denied hop, aborting the fetch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _response_dict(resp: Any) -> dict:
    headers = dict(resp.headers.items()) if hasattr(resp, "headers") else {}
    raw = read_capped(resp)  # bounded read — unbounded-body DoS guard (#1913 class)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        body = raw.decode("utf-8", errors="replace")
    status = getattr(resp, "status", None) or resp.getcode()
    return {"status": int(status), "body": body, "headers": headers}


def _request(
    method: str,
    url: str,
    *,
    body: Any = None,
    json_body: bool = True,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    _check_host(url)
    data: bytes | None = None
    hdrs: dict[str, str] = dict(headers or {})
    if body is not None:
        if json_body:
            data = _json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = str(body).encode("utf-8")
    req = _Request(url, data=data, headers=hdrs, method=method)
    # #1956: open via an opener whose redirect handler re-gates each hop (L1+L2),
    # replacing the stock global opener that followed redirects without re-check.
    opener = _build_opener(_SSRFSafeRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — see module docstring
            return _response_dict(resp)
    except _HTTPError as exc:
        return _response_dict(exc)


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP GET. Returns ``{status, body, headers}``."""
    return _request("GET", url, headers=headers, timeout=timeout)


def post(
    url: str,
    body: Any,
    *,
    json_body: bool = True,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP POST. Returns ``{status, body, headers}``."""
    return _request(
        "POST", url, body=body, json_body=json_body, headers=headers, timeout=timeout
    )


def put(
    url: str,
    body: Any,
    *,
    json_body: bool = True,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP PUT. Returns ``{status, body, headers}``."""
    return _request(
        "PUT", url, body=body, json_body=json_body, headers=headers, timeout=timeout
    )


def delete(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP DELETE. Returns ``{status, body, headers}``."""
    return _request("DELETE", url, headers=headers, timeout=timeout)
