"""Shared HTTP response-body byte ceiling (unbounded-body DoS guard, #1913 class).

Single source of truth for the download-byte cap used by the urllib-based HTTP
helpers (``reyn.api.safe.http`` / ``reyn.api.unsafe.http`` / ``reyn.mcp.registry``)
and the ``web.fetch.max_download_bytes`` config default. Stdlib-only (no reyn
imports) so any layer — config, api, mcp — can import it without a cycle.

A bare ``response.read()`` materialises the ENTIRE body into memory, so a
hostile / compromised / redirecting endpoint can exhaust memory. ``read_capped``
reads at most ``max_bytes + 1`` and rejects an over-limit body before more than
the ceiling is ever materialised. (web_fetch uses an equivalent streaming
ceiling — #1925; this is the urllib-adapted sibling.)
"""
from __future__ import annotations

from typing import Any

# 10 MiB — THE single source for the HTTP response-body ceiling. Referenced by
# the urllib HTTP helpers and the web.fetch.max_download_bytes config default so
# the value is defined exactly once.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class ResponseTooLargeError(ValueError):
    """Raised when a response body exceeds the download-byte ceiling.

    Subclasses :class:`ValueError` so existing ``except ValueError`` paths still
    treat it as a malformed/oversized response.
    """


def read_capped(resp: Any, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Read a urllib/file-like response body, bounded to ``max_bytes``.

    Reads at most ``max_bytes + 1`` bytes (so an over-limit body is detected
    without ever materialising more than the ceiling) and raises
    :class:`ResponseTooLargeError` if the body exceeds ``max_bytes``.
    """
    raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ResponseTooLargeError(
            f"response body exceeds the {max_bytes}-byte ceiling "
            "(unbounded-memory guard)"
        )
    return raw
