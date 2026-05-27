"""Persistent MCP tools cache file utilities — FP-0037 S1/S2.

Cache file location: ``<state_dir>/mcp_tools_cache.json``

The cache stores per-server tool lists so ``reyn mcp refresh`` can write
fresh probe results and active ``reyn chat`` sessions can warm-start on
the next turn without a live probe.

Format (version 1):
    {
        "version": 1,
        "probed_at": "<ISO-8601 UTC>",
        "servers": {
            "<server_name>": [
                {"name": "tool_name", "description": "...", "inputSchema": {...}},
                ...
            ]
        }
    }

Public API:
    cache_file_path(state_dir)           -> Path
    write_cache(path, servers)           -> None  (atomic via .tmp + os.replace)
    read_cache(path)                     -> dict[str, list[dict]] | None
    file_mtime(path)                     -> float | None
    yaml_scope_paths(project_root)       -> list[Path]   (FP-0037 S2)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_VERSION = 1
_CACHE_FILENAME = "mcp_tools_cache.json"


def cache_file_path(state_dir: Path) -> Path:
    """Return the canonical cache file path for the given state directory.

    Does NOT create the directory — callers that need the dir to exist
    must call ``write_cache`` which creates it on demand.
    """
    return Path(state_dir) / _CACHE_FILENAME


def write_cache(path: Path, servers: dict[str, list[dict]]) -> None:
    """Atomically write the MCP tools cache to ``path``.

    Creates the parent directory if it does not exist.  Uses a ``.tmp``
    sibling + ``os.replace`` so readers never see a partial write.

    Parameters
    ----------
    path:
        Target file path (e.g. ``.reyn/state/mcp_tools_cache.json``).
    servers:
        Mapping of server name → list of tool dicts.  Must be
        JSON-serialisable; the caller is responsible for ensuring the
        tool dicts contain only JSON-safe types.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CACHE_VERSION,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "servers": servers,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_cache(path: Path) -> dict[str, list[dict]] | None:
    """Read the MCP tools cache from ``path``.

    Returns the ``servers`` dict on success, or ``None`` on any failure:
    - File absent → ``None`` (silent).
    - File corrupt (bad JSON or unexpected structure) → ``None`` + warning.
    - Version mismatch → ``None`` (silent; future migration is caller's job).

    Never raises.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_cache_file: cannot parse %s: %r", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("mcp_cache_file: unexpected root type in %s", path)
        return None
    if data.get("version") != _CACHE_VERSION:
        return None
    servers = data.get("servers")
    if not isinstance(servers, dict):
        logger.warning("mcp_cache_file: missing or malformed 'servers' in %s", path)
        return None
    return servers


def file_mtime(path: Path) -> float | None:
    """Return the file's last-modified time as a Unix timestamp, or None.

    Returns ``None`` when the file does not exist (or cannot be stat'd).
    Never raises.
    """
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def yaml_scope_paths(project_root: "Path | None") -> list[Path]:
    """Return the ordered list of MCP yaml config paths for the 3 scope tiers.

    FP-0037 S2: shared helper used by ``RouterHostAdapter.maybe_refresh_mcp_tools_from_yaml``
    and (as a follow-up) by ``cli/commands/mcp.py``.

    Tiers (matching ``reyn mcp list`` priority, lowest → highest):
      1. user-global: ``~/.reyn/config.yaml``    (always included)
      2. project:     ``<project_root>/reyn.yaml``       (when project_root is not None)
      3. project-local: ``<project_root>/reyn.local.yaml`` (when project_root is not None)

    Only the *potential* paths are returned — callers are responsible for
    checking existence before reading.  The list never includes a path for
    scopes that cannot be resolved (i.e. project/local when project_root
    is None).

    Never raises.
    """
    paths: list[Path] = []
    paths.append(Path.home() / ".reyn" / "config.yaml")
    if project_root is not None:
        root = Path(project_root)
        paths.append(root / "reyn.yaml")
        paths.append(root / "reyn.local.yaml")
    return paths
