"""MemoryService — per-session memory persistence helpers
(extracted from ChatSession wave 3 PR2).

Stateless service that centralises the path-resolution + file-op orchestration
for the remember / forget / read_body operations that previously lived on
ChatSession.  All file I/O goes through injected async callbacks so the
permission boundary (OpContext) is never bypassed — MemoryService intentionally
knows nothing about op_runtime, OpContext, Workspace, or PermissionResolver.
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from reyn.core.events.events import EventLog


class MemoryService:
    """Stateless memory-entry persistence adapter.

    Paths derive from ``agent_workspace_dir`` + the ``layer`` argument per
    call.  There is no mutable per-call state.

    Parameters
    ----------
    agent_workspace_dir:
        ``Path`` pointing to ``.reyn/agents/<agent_name>``.  Used to resolve
        the ``"agent"`` layer directory.
    events:
        The session's ``EventLog``.  Used to emit ``memory_saved`` and
        ``memory_deleted`` events.
    file_write:
        Async callback ``(path: str, content: str) -> dict``.
        Returns ``{"path": ..., "written": True}`` or ``{"error": ...}``.
    file_read:
        Async callback ``(path: str) -> dict``.
        Returns ``{"path": ..., "content": str}`` or ``{"error": ...}``.
    file_delete:
        Async callback ``(path: str) -> dict``.
        Returns ``{"path": ..., "deleted": bool}`` or ``{"error": ...}``.
    file_regenerate_index:
        Async callback ``(*, path, output_path, entry_template, header) -> dict``.
        Returns ``{"path": ..., "output_path": ..., "entries": int}`` or
        ``{"error": ...}``.
    """

    def __init__(
        self,
        *,
        agent_workspace_dir,                     # Path; = .reyn/agents/<agent>
        events: EventLog,
        file_write: Callable[..., Awaitable[dict]],
        file_read: Callable[..., Awaitable[dict]],
        file_delete: Callable[..., Awaitable[dict]],
        file_regenerate_index: Callable[..., Awaitable[dict]],
    ) -> None:
        self._workspace = Path(agent_workspace_dir)
        self._events = events
        self._file_write = file_write
        self._file_read = file_read
        self._file_delete = file_delete
        self._file_regenerate_index = file_regenerate_index

    # ── Path helpers ─────────────────────────────────────────────────────────

    def memory_dir(self, layer: str) -> str:
        """Directory for the memory layer.

        layer="shared" → .reyn/memory
        layer="agent"  → .reyn/agents/<agent_name>/memory
        """
        if layer == "shared":
            return str(Path(".reyn") / "memory")
        return str(self._workspace / "memory")

    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer + slug to file path.

        layer="shared" → .reyn/memory/<slug>.md
        layer="agent"  → .reyn/agents/<agent_name>/memory/<slug>.md
        """
        return str(Path(self.memory_dir(layer)) / f"{slug}.md")

    # ── Async ops ─────────────────────────────────────────────────────────────

    async def remember(
        self,
        *,
        layer: str,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> dict:
        """Persist a memory entry.

        Constructs YAML frontmatter, writes the body file, then regenerates
        the layer's ``MEMORY.md`` index.

        Returns ``{"saved": slug, "layer": layer, "path": <path>}``
        or ``{"error": <reason>}`` on failure.
        """
        mem_dir = self.memory_dir(layer)
        body_path = self.memory_path(layer, slug)

        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {type}\n"
            f"---\n"
        )
        full_content = frontmatter + body

        write_result = await self._file_write(body_path, full_content)
        if "error" in write_result:
            return {"error": write_result["error"]}

        index_path = str(Path(mem_dir) / "MEMORY.md")
        regen_result = await self._file_regenerate_index(
            path=mem_dir,
            output_path=index_path,
            entry_template="- [{name}]({slug}.md) — {description}",
            header="# Memory Index\n\n",
        )
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._events.emit(
            "memory_saved", layer=layer, slug=slug, path=body_path,
        )
        return {"saved": slug, "layer": layer, "path": body_path}

    async def forget(self, *, layer: str, slug: str) -> dict:
        """Delete a memory entry and regenerate the index.

        Returns ``{"deleted": slug, "layer": layer}``
        or ``{"error": <reason>}`` if the entry was not found.
        """
        body_path = self.memory_path(layer, slug)
        del_result = await self._file_delete(body_path)
        if "error" in del_result:
            return {"error": del_result["error"]}
        if not del_result.get("deleted"):
            return {"error": f"memory entry not found: {slug}"}

        mem_dir = self.memory_dir(layer)
        index_path = str(Path(mem_dir) / "MEMORY.md")
        regen_result = await self._file_regenerate_index(
            path=mem_dir,
            output_path=index_path,
            entry_template="- [{name}]({slug}.md) — {description}",
            header="# Memory Index\n\n",
        )
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._events.emit("memory_deleted", layer=layer, slug=slug, path=body_path)
        return {"deleted": slug, "layer": layer}

    async def read_body(self, *, layer: str, slug: str) -> dict:
        """Read a memory body file's contents.

        Returns ``{"layer": layer, "slug": slug, "content": <text>}``
        or ``{"error": <reason>}`` if not found.
        """
        body_path = self.memory_path(layer, slug)
        result = await self._file_read(body_path)
        if "error" in result:
            return {"error": result["error"]}
        return {"layer": layer, "slug": slug, "content": result["content"]}


__all__ = ["MemoryService"]
