"""Config-recovery emit helper — the single producer-side seam for ``config_changed``.

#2248 PR-A2. A recovery-core `.reyn/config` registry (mcp / cron / hooks / approvals /
index-sources) is reconstructed by WAL replay (``AgentRegistry._reconcile_config_as_of_cut``).
The producer side is this one helper: a dedicated config op calls it AFTER persisting its
`.yaml`, passing the registry's `.reyn`-relative path + its FULL post-mutation content, so the
yaml becomes a derived projection and the WAL event is the recovery truth. Centralised here so
every emit site (and the registry's own ``record_config_change``) shares one format — the
config-change vocabulary lives in exactly one place.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog


def reyn_relative_path(path) -> str | None:
    """The portion of a config path BELOW the project ``.reyn/`` dir — the key a
    ``config_changed`` event uses (reconstruct writes ``content`` back to
    ``<.reyn>/<rel>``). E.g. ``…/.reyn/mcp.yaml`` → ``mcp.yaml``;
    ``…/.reyn/config/mcp.yaml`` → ``config/mcp.yaml`` (robust across the #2248 §6 reorg).
    None when the path is not under a ``.reyn/`` dir (operator-owned / out of recovery-core
    → not WAL-tracked)."""
    parts = Path(path).parts
    if ".reyn" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index(".reyn")  # the LAST `.reyn` component
    rel = parts[i + 1:]
    return "/".join(rel) if rel else None


async def record_config_change(
    state_log: "StateLog | None", rel_path: str, content: dict,
) -> None:
    """Emit a durable ``config_changed`` for the registry at ``rel_path`` (`.reyn`-relative)
    carrying its FULL post-mutation ``content``. No-op when ``state_log`` is None (the opt-in /
    non-persistence contract — tests / non-chat). Call it AFTER the `.yaml` is persisted."""
    if state_log is None:
        return
    await state_log.append("config_changed", path=rel_path, content=content)
