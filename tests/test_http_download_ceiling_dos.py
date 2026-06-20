"""Tier 2: HTTP response-body download ceiling (unbounded-body DoS, #1913 class).

The urllib HTTP helpers (`reyn.api.safe.http` / `reyn.api.unsafe.http` /
`reyn.mcp.registry`) read response bodies with a single shared `read_capped`
seam (`reyn._http_limits`) so a hostile / oversized response is rejected before
more than `MAX_DOWNLOAD_BYTES` is materialised — the urllib-adapted sibling of
the web_fetch ceiling (#1925). One mechanism, single-sourced constant.

Policy: real `read_capped` + real `reyn.api.safe.http` (its `_urlopen` is the
network boundary, patched with a Fake response — no MagicMock of reyn code).
Tier line first.
"""
from __future__ import annotations

import pytest

import reyn.api.safe.http as safe_http
from reyn._http_limits import (
    MAX_DOWNLOAD_BYTES,
    ResponseTooLargeError,
    read_capped,
)


class _FakeResp:
    """Minimal urllib-response stand-in: ``read(amt)`` returns up to ``amt``
    bytes of a fixed body; supports the ``with`` protocol + ``headers``/``status``."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.headers = {}
        self.status = status

    def read(self, amt: int | None = None) -> bytes:
        return self._body[:amt] if amt is not None else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── read_capped (the single seam) ────────────────────────────────────────────

def test_read_capped_under_limit_returns_body():
    """Tier 2: a body within the cap is returned in full."""
    assert read_capped(_FakeResp(b"x" * 50), max_bytes=100) == b"x" * 50


def test_read_capped_at_limit_returns_body():
    """Tier 2: a body exactly at the cap is allowed (boundary)."""
    assert read_capped(_FakeResp(b"x" * 100), max_bytes=100) == b"x" * 100


def test_read_capped_over_limit_rejects():
    """Tier 2: a body over the cap raises ResponseTooLargeError without
    materialising more than the ceiling+1."""
    with pytest.raises(ResponseTooLargeError):
        read_capped(_FakeResp(b"x" * 1000), max_bytes=100)


def test_default_max_is_single_sourced_with_web_fetch():
    """Tier 2: the helper default and the web.fetch config default are the SAME
    constant (single-source, no magic-number duplication)."""
    from reyn.config.media import WebFetchConfig
    assert WebFetchConfig().max_download_bytes == MAX_DOWNLOAD_BYTES


# ── integration: safe.http rejects an oversized body (falsifiable) ───────────

def test_safe_http_rejects_oversized_body(monkeypatch):
    """Tier 2: reyn.api.safe.http rejects a response body over the ceiling — the
    bounded read prevents unbounded materialisation. (Pre-fix this returned the
    whole body with no cap.)"""
    safe_http._set_permission_context(http_hosts=["example.com"])
    huge = b"A" * (MAX_DOWNLOAD_BYTES + 1024)
    monkeypatch.setattr(safe_http, "_urlopen", lambda *a, **k: _FakeResp(huge))
    with pytest.raises(ResponseTooLargeError):
        safe_http.get("https://example.com/huge")


def test_safe_http_allows_normal_body(monkeypatch):
    """Tier 2: (regression) a normal-sized body still returns the full envelope."""
    safe_http._set_permission_context(http_hosts=["example.com"])
    monkeypatch.setattr(safe_http, "_urlopen", lambda *a, **k: _FakeResp(b"hello"))
    out = safe_http.get("https://example.com/ok")
    assert out["status"] == 200
    assert out["body"] == "hello"
