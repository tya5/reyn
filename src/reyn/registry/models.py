"""Data models for the MCP registry client.

ServerInfo  — lightweight search-result entry (name / description / repo URL /
              runtime hint derived from packages).
ServerJson  — richer server.json reflection used when installing a server.

Both are forward-compatible: unknown fields in the registry response are
silently ignored so schema additions don't break the client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServerInfo:
    """Lightweight descriptor returned by registry search.

    Fields mirror the registry ``/v0.1/servers`` response envelope:
      - ``name``        — registry identifier, e.g. ``"io.github.foo/bar-mcp"``
      - ``description`` — one-line description from server.json
      - ``repository_url`` — GitHub (or other) repository URL
      - ``runtime_hint``   — inferred from packages[0].registryType: one of
                             ``"npx"`` / ``"uvx"`` / ``"docker"`` / ``"dnx"`` / ``""``
    """

    name: str
    description: str
    repository_url: str
    runtime_hint: str = ""


@dataclass
class ServerPackage:
    """One entry from server.json ``packages[]``."""

    registry_type: str          # "npm" / "pypi" / "docker" / "nuget"
    identifier: str             # e.g. "@modelcontextprotocol/server-github"
    version: str = ""
    transport_type: str = ""    # "stdio" / "streamable-http" etc.
    # Raw dict for forward-compat — callers may inspect unknown fields.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerRemote:
    """One entry from server.json ``remotes[]``."""

    type: str                   # "streamable-http" / "sse"
    url: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerJson:
    """Full server.json reflection.

    Only fields common enough to be useful are promoted to typed attributes.
    Everything else stays in ``raw`` for forward-compat.
    """

    name: str
    description: str
    version: str
    repository_url: str
    schema_url: str             # "$schema" field — used for skew detection
    packages: list[ServerPackage] = field(default_factory=list)
    remotes: list[ServerRemote] = field(default_factory=list)
    website_url: str = ""
    # Raw server.json dict for callers that need unpromoted fields.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def runtime_hint(self) -> str:
        """Infer runtime hint from the first package's registryType.

        Mapping (= registry schema 4 entries):
          npm    → npx
          pypi   → uvx
          docker → docker
          nuget  → dnx
        Returns ``""`` if packages is empty or registryType is unknown.
        """
        _MAP = {"npm": "npx", "pypi": "uvx", "docker": "docker", "nuget": "dnx"}
        for pkg in self.packages:
            hint = _MAP.get(pkg.registry_type.lower())
            if hint:
                return hint
        return ""


# ---------------------------------------------------------------------------
# Helpers for constructing models from raw registry response dicts
# ---------------------------------------------------------------------------

def _runtime_hint_from_packages(packages_raw: list[dict[str, Any]]) -> str:
    """Derive runtime hint from packages list (for ServerInfo)."""
    _MAP = {"npm": "npx", "pypi": "uvx", "docker": "docker", "nuget": "dnx"}
    for pkg in packages_raw:
        rt = pkg.get("registryType", "").lower()
        hint = _MAP.get(rt)
        if hint:
            return hint
    return ""


def server_info_from_raw(entry: dict[str, Any]) -> ServerInfo:
    """Build a ``ServerInfo`` from one element of ``/v0.1/servers`` response.

    ``entry`` is ``{"server": {...}, "_meta": {...}}``.
    """
    srv = entry.get("server", entry)  # tolerate bare server dict
    packages_raw: list[dict[str, Any]] = srv.get("packages", [])
    repo = srv.get("repository", {})
    return ServerInfo(
        name=srv.get("name", ""),
        description=srv.get("description", ""),
        repository_url=repo.get("url", ""),
        runtime_hint=_runtime_hint_from_packages(packages_raw),
    )


def server_json_from_raw(srv: dict[str, Any]) -> ServerJson:
    """Build a ``ServerJson`` from a raw server.json dict.

    ``srv`` may be the ``server`` sub-key from the versions/latest endpoint
    or a bare server.json object.
    """
    packages_raw: list[dict[str, Any]] = srv.get("packages", [])
    packages = [
        ServerPackage(
            registry_type=p.get("registryType", ""),
            identifier=p.get("identifier", ""),
            version=p.get("version", ""),
            transport_type=(p.get("transport") or {}).get("type", ""),
            raw=p,
        )
        for p in packages_raw
    ]
    remotes_raw: list[dict[str, Any]] = srv.get("remotes", [])
    remotes = [
        ServerRemote(
            type=r.get("type", ""),
            url=r.get("url", ""),
            raw=r,
        )
        for r in remotes_raw
    ]
    repo = srv.get("repository", {})
    return ServerJson(
        name=srv.get("name", ""),
        description=srv.get("description", ""),
        version=srv.get("version", ""),
        repository_url=repo.get("url", ""),
        schema_url=srv.get("$schema", ""),
        website_url=srv.get("websiteUrl", ""),
        packages=packages,
        remotes=remotes,
        raw=srv,
    )
