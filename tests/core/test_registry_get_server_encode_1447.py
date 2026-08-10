"""Tier 2: #1447 — registry get_server percent-encodes the reverse-DNS id.

Registry IDs are reverse-DNS (e.g. ``io.github.foo/bar``) and always contain
``/``. ``get_server`` built ``/v0.1/servers/{id}/versions/latest`` without
encoding, so the ``/`` split into extra path segments → the registry 404'd →
every registry-based ``mcp install`` failed (#221 dogfood). This pins the URL
shape (network-free: the real HTTP ``_get`` is captured, not called).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.registry.client import RegistryClient


class _CapturedPath(Exception):
    """Sentinel raised after the URL path is captured (short-circuits the call;
    not a RegistryError, so get_server's retry loop doesn't swallow it)."""


def _captured_get_path(server_id: str, monkeypatch) -> str:
    captured: dict[str, str] = {}

    async def _recording_get(self, path, base_url=None, params=None):
        captured["path"] = path
        raise _CapturedPath

    # Use a unique id per call so the response cache can't short-circuit _get.
    monkeypatch.setattr(RegistryClient, "_get", _recording_get)
    with pytest.raises(_CapturedPath):
        asyncio.run(RegistryClient().get_server(server_id))
    return captured["path"]


def test_get_server_percent_encodes_reverse_dns_id(monkeypatch):
    """Tier 2: #1447 — a reverse-DNS id (with '/') is percent-encoded into ONE
    path segment. Falsifiable: drop the quote() and the raw '/' id appears."""
    path = _captured_get_path("io.github.test1447/encode-probe-xyz", monkeypatch)
    assert "io.github.test1447%2Fencode-probe-xyz" in path
    # the raw '/'-bearing id must NOT appear (that was the 404 bug)
    assert "io.github.test1447/encode-probe-xyz" not in path
    # still the right endpoint shape
    assert path.startswith("/v0.1/servers/")
    assert path.endswith("/versions/latest")


def test_get_server_plain_id_has_no_encoded_slash(monkeypatch):
    """Tier 2: #1447 — an id with no '/' is unaffected (encoding is a no-op for
    it) — the fix doesn't mangle plain ids."""
    path = _captured_get_path("plainid-no-slash-1447", monkeypatch)
    assert "plainid-no-slash-1447" in path
    assert "%2F" not in path
