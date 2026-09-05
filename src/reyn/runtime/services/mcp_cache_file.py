"""Persistent MCP tools cache file utilities — FP-0037 S1/S2.

Cache file location: ``<state_dir>/mcp_tools_cache.json``

The cache stores per-server tool lists so ``reyn mcp refresh`` can write
fresh probe results and active ``reyn chat`` sessions can warm-start on
the next turn without a live probe.

#3520 — **the cache stores ANSWERS, never non-answers.** A probe that
timed out or raised did not measure "this server has zero tools"; it
measured nothing at all. Storing that as ``[]`` made the two
indistinguishable, and because the cache is permanent-by-design the
non-answer then outlived the condition that produced it: the model was
never told about tools the server actually had, for the rest of the
session AND (once written here) across restarts. The fix is in the type
— ``ProbeOutcome`` is a discriminated union of ``ToolsAnswered`` and
``ToolsUnknown``, and only ``ToolsAnswered`` is storable. An unknown is
simply absent from the mapping, so it is naturally re-probed the next
time it is needed. An empty ``ToolsAnswered.tools`` means, and only
means, "measured: this server exposes zero tools".

#4401 A-4 co-vet (F2, load-bearing — read this before "optimizing" what
gets persisted here) — **this file is shared by every session in the same
workspace** (``RouterHostAdapter._state_dir`` defaults to
``cwd/.reyn/state``, one file, N sessions, N independent in-memory
epochs). "Every writer re-reads the file's current mtime before writing"
(true of both writers today) does NOT by itself prevent a LOST UPDATE
across that shared file — two sessions can both read, then whichever
writes second silently clobbers the answers the other just wrote. What
actually keeps a clobbered answer from being silently wrong FOREVER is
the property this module's own #3520 paragraph above states: **an absent
server is simply re-probed the next time it's needed.** A clobbered
answer is temporarily MISSING, not permanently wrong — self-healing, not
correctness, and that distinction is exactly why the type-level "only
``ToolsAnswered`` is storable" rule matters beyond #3520's own original
motivation. If a future change ever persists ``ToolsUnknown`` too (e.g.
as a "let's skip re-probing known-failed servers" optimization), it
silently removes the ONE property this cross-session tolerance depends
on — a lost update would then stay lost, not self-heal. Do not make that
change without re-deriving this paragraph's own conclusion first.

Format (version 2):
    {
        "version": 2,
        "probed_at": "<ISO-8601 UTC>",
        "servers": {
            "<server_name>": [
                {"name": "tool_name", "description": "...", "inputSchema": {...}},
                ...
            ]
        }
    }

K: the version bump from 1 to 2 is load-bearing and must not be reverted
to "just read version 1 too". A version-1 file's ``[]`` entries are
IRREVERSIBLY AMBIGUOUS — the writer that produced them could not
distinguish "answered zero" from "could not measure", so nothing read
back from such a file can recover the distinction. Rather than guess,
``read_cache`` declines a version-1 file wholesale (the existing
version-mismatch branch): the affected servers are re-probed once, and
the file is rewritten as version 2 where ``[]`` is unambiguous.

Public API:
    cache_file_path(state_dir)           -> Path
    write_cache(path, servers)           -> None  (atomic via .tmp + os.replace)
    read_cache(path)                     -> dict[str, ToolsAnswered] | None
    answered_only(results)               -> dict[str, ToolsAnswered]
    file_mtime(path)                     -> float | None
    yaml_scope_paths(project_root)       -> list[Path]   (FP-0037 S2)
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_CACHE_VERSION = 2
_CACHE_FILENAME = "mcp_tools_cache.json"


@dataclass(frozen=True)
class ToolsAnswered:
    """An MCP tools probe that ANSWERED — ``tools`` is the measurement.

    ``tools == []`` is a real answer ("this server exposes no tools"), not
    a stand-in for failure; failure is ``ToolsUnknown``.
    """

    tools: list[dict] = field(default_factory=list)
    kind: Literal["answered"] = "answered"


@dataclass(frozen=True)
class ToolsUnknown:
    """An MCP tools probe that did NOT answer — nothing was measured.

    ``reason`` mirrors the ``mcp_tool_probe_degraded`` audit-event's
    ``reason`` field (``"timeout"`` / ``"exception"``) so the operator-facing
    trace and the in-process value agree. This variant is deliberately not
    storable: ``write_cache`` accepts only ``ToolsAnswered``, so an unknown
    cannot reach disk and cannot survive a restart.
    """

    reason: str
    detail: str | None = None
    kind: Literal["unknown"] = "unknown"


ProbeOutcome = ToolsAnswered | ToolsUnknown


def answered_only(
    results: Iterable[tuple[str, ProbeOutcome]],
) -> dict[str, ToolsAnswered]:
    """Keep the answers, drop the unknowns.

    The single funnel every probe result passes through before it can be
    cached (in memory or on disk). Dropping — rather than storing a
    placeholder — is what makes an unknown re-probed next time it is
    needed, because "absent" is the only representation of "not measured"
    that the cache has.
    """
    return {
        name: outcome
        for name, outcome in results
        if isinstance(outcome, ToolsAnswered)
    }


def cache_file_path(state_dir: Path) -> Path:
    """Return the canonical cache file path for the given state directory.

    Does NOT create the directory — callers that need the dir to exist
    must call ``write_cache`` which creates it on demand.
    """
    return Path(state_dir) / _CACHE_FILENAME


def write_cache(path: Path, servers: dict[str, ToolsAnswered]) -> None:
    """Atomically write the MCP tools cache to ``path``.

    Creates the parent directory if it does not exist.  Uses a ``.tmp``
    sibling + ``os.replace`` so readers never see a partial write.

    Parameters
    ----------
    path:
        Target file path (e.g. ``.reyn/state/mcp_tools_cache.json``).
    servers:
        Mapping of server name → ``ToolsAnswered``.  #3520: the parameter
        type is the gate — a ``ToolsUnknown`` cannot be written, so a
        probe that failed cannot leave a permanent "this server has no
        tools" record behind. Route probe results through
        ``answered_only()`` to obtain this mapping.  Tool dicts must be
        JSON-serialisable; the caller is responsible for that.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CACHE_VERSION,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "servers": {name: entry.tools for name, entry in servers.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_cache(path: Path) -> dict[str, ToolsAnswered] | None:
    """Read the MCP tools cache from ``path``.

    Returns ``{server: ToolsAnswered}`` on success, or ``None`` on any failure:
    - File absent → ``None`` (silent).
    - File corrupt (bad JSON or unexpected structure) → ``None`` + warning.
    - Version mismatch → ``None`` (silent). #3520: this is also the migration
      path for version-1 files, whose ``[]`` entries cannot be told apart
      from failed probes — see the module docstring's K note.

    A per-server entry that is not a list is dropped (rather than failing the
    whole file), because one malformed server should not cost the others their
    answers; the dropped server is then simply "not measured" and re-probed.

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
    return {
        str(name): ToolsAnswered(tools=tools)
        for name, tools in servers.items()
        if isinstance(tools, list)
    }


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
