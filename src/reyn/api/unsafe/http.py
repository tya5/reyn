"""HTTP helpers for ``unsafe``-mode python steps.

Scope A (= this FP): runs inside the python step's subprocess and
uses ``urllib.request`` directly (= no extra dependency).
Permission for network access was granted at parent level when
the step's ``mode: unsafe`` was approved at startup; individual
calls are NOT audited per-invocation (= step-level audit only).
For finer audit see FP-0015 (deferred).

Every function returns a ``{"status": int, "body": str, "headers":
dict[str, str]}`` envelope. HTTP error statuses (4xx / 5xx) are
NOT raised — callers inspect ``status``.
"""

from __future__ import annotations

import json as _json
from typing import Any
from urllib.error import HTTPError as _HTTPError
from urllib.request import Request as _Request, urlopen as _urlopen


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
        with _urlopen(req, timeout=timeout) as resp:  # noqa: S310 - intentional, unsafe mode
            return _response_dict(resp)
    except _HTTPError as exc:
        return _response_dict(exc)


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP GET. Returns ``{status, body, headers}``.

    Step-level audit (Scope A) — see module docstring.
    """
    return _request("GET", url, headers=headers, timeout=timeout)


def post(
    url: str,
    body: Any,
    *,
    json_body: bool = True,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP POST. Returns ``{status, body, headers}``.

    Step-level audit (Scope A) — see module docstring.
    """
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
    """HTTP PUT. Returns ``{status, body, headers}``.

    Step-level audit (Scope A) — see module docstring.
    """
    return _request(
        "PUT", url, body=body, json_body=json_body, headers=headers, timeout=timeout
    )


def delete(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict:
    """HTTP DELETE. Returns ``{status, body, headers}``.

    Step-level audit (Scope A) — see module docstring.
    """
    return _request("DELETE", url, headers=headers, timeout=timeout)
