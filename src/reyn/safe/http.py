"""Safe-mode HTTP for stdlib skills (FP-0042 Phase 3 drift-fix).

Exposes the same ``get`` / ``post`` / ``put`` / ``delete`` surface as
``reyn.api.unsafe.http`` (= ``urllib.request``-backed, no extra deps),
but in the ``reyn.safe.*`` namespace so safe-mode python steps can
import it through the AST allowlist.

Threat model + permission rationale
-----------------------------------

This module has **no per-call permission gate**. The "safe" label here
matches the namespace's role (= AST-allowlisted, callable from safe-mode
code) rather than the stronger per-call permission-resolver pattern
that :mod:`reyn.safe.file` enforces.

This is the pragmatic landing point for the cascade of FP-0042 Phase
3-class skills that fetch from public registries (= ``mcp_install`` /
``mcp_search`` already use the domain-specific
:mod:`reyn.safe.mcp.registry`; this module covers the broader
``skill_search`` + ``skill_importer`` family that fetches from GitHub
raw / Contents endpoints).

The architectural decision on **whether** HTTP needs a permission gate
(and what shape it should take — bool flag, host allowlist, capability
axis) is captured at `Issue #571
<https://github.com/tya5/reyn/issues/571>`_ ("Permission model:
granularity decomposition vs abstraction granularity"). When that
discussion lands, this module's surface should be revisited; until
then, ``reyn.safe.http`` is operationally equivalent to
``reyn.api.unsafe.http`` (same urllib wiring, same envelope shape) and
exists only to keep stdlib skills inside the safe-mode AST allowlist.

Internal layering
-----------------

This module is reyn-package internal code (= not subject to the
safe-mode AST validator). It freely imports ``urllib.request`` /
``urllib.error``; the validator only rejects user-code imports outside
the allowlist, and ``reyn.safe.*`` is admitted.

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
from urllib.request import Request as _Request
from urllib.request import urlopen as _urlopen


def _response_dict(resp: Any) -> dict:
    headers = dict(resp.headers.items()) if hasattr(resp, "headers") else {}
    raw = resp.read()
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
    try:
        with _urlopen(req, timeout=timeout) as resp:  # noqa: S310 — see module docstring
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
