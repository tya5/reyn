"""reyn.registry — MCP server registry client and cache.

Public API:
  RegistryClient  — async HTTP client (context manager).
  RegistryError   — raised on network / HTTP failures.
  ServerInfo      — lightweight search result (name / description / repo_url / runtime_hint).
  ServerJson      — full server.json reflection.
  cache           — file-based TTL cache module (get / set).

Quick usage::

    from reyn.registry import RegistryClient, RegistryError

    async with RegistryClient() as client:
        results = await client.search("slack")
        for info in results:
            print(info.name, info.description)
"""
from reyn.registry.client import RegistryClient, RegistryError
from reyn.registry.models import ServerInfo, ServerJson, ServerPackage, ServerRemote

__all__ = [
    "RegistryClient",
    "RegistryError",
    "ServerInfo",
    "ServerJson",
    "ServerPackage",
    "ServerRemote",
]
