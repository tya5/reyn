"""Config-recovery producer seam — record a config registry as a truncation-surviving snapshot.

#2259 PR-1. A recovery-core `.reyn/config` registry (mcp / cron / hooks / index-sources) is
reconstructed by restoring the latest config GENERATION ≤ the rewind target. The producer
side is this one helper: a dedicated config op calls it AFTER persisting its `.yaml`, passing
the persisted absolute path + its FULL post-mutation content; the helper writes a full-state
generation keyed by the current WAL head (a base that survives WAL truncation — unlike the
former `config_changed` WAL event, which the truncation could silently drop). Centralised
here so every emit site shares one path-key + seq-key convention.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog


def reyn_relative_path(path) -> str | None:
    """The portion of a config path BELOW the project ``.reyn/`` dir — the key a config
    generation is filed under (reconstruct restores ``content`` back to ``<.reyn>/<rel>``).
    E.g. ``…/.reyn/config/mcp.yaml`` → ``config/mcp.yaml``. None when the path is not under a
    ``.reyn/`` dir (operator-owned / out of recovery-core → not tracked)."""
    parts = Path(path).parts
    if ".reyn" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index(".reyn")  # the LAST `.reyn` component
    rel = parts[i + 1:]
    return "/".join(rel) if rel else None


def reyn_root(path) -> "Path | None":
    """The ``.reyn/`` directory an absolute config path lives under (the generation-store
    anchor: generations go in ``<.reyn>/config/generations/``)."""
    p = Path(path)
    for ancestor in (p, *p.parents):
        if ancestor.name == ".reyn":
            return ancestor
    return None


def config_generations_dir(reyn_dir: "Path") -> "Path":
    """The config-generation store directory under a ``.reyn/`` dir."""
    return Path(reyn_dir) / "config" / "generations"


async def record_config_generation(
    state_log: "StateLog | None", config_abs_path, content: dict,
) -> None:
    """Record the FULL config state as a generation keyed by the DURABLE WAL head
    (``last_durable_seq``) — the truncation-surviving recovery base for this registry. #2259
    PR-2b: keyed at the DURABLE watermark, not the live ``current_seq`` — a config op has no WAL
    entry of its own (it emits a P6 audit event, not ``state_log.append``), so it tags the
    durable WAL position it is consistent with; keying at a non-durable seq would let a crash
    leave the config-gen referencing a WAL position past the durable tail (a hole, the truncation
    class). Two config ops between WAL entries share the same durable seq → the 2nd overwrites
    the 1st at ``{rel}@{seq}.yaml``; harmless because config-gen stores FULL post-state (the
    latest write at that seq is the complete correct config; no distinct rewind target is lost).
    No-op when ``state_log`` is None (the opt-in / non-persistence contract) or the path is not
    under ``.reyn/``. Call it AFTER the `.yaml` is persisted."""
    if state_log is None:
        return
    rel = reyn_relative_path(config_abs_path)
    root = reyn_root(config_abs_path)
    if rel is None or root is None:
        return
    from reyn.core.events.config_generations import ConfigGenerationStore  # noqa: PLC0415
    ConfigGenerationStore(config_generations_dir(root)).record(
        rel, content, state_log.last_durable_seq,
    )
