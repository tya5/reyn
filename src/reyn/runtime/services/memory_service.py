"""MemoryService — the session's memory-store capability.

``remember`` / ``forget`` / ``read_body`` are memory-layer operations, not
file operations: each carries domain rules (a threat scan that REJECTS a
poisoned write before it persists, YAML frontmatter construction and
stripping, listing-index regeneration, knowledge-index ingest/de-index) that
belong with the memory domain rather than with whatever loop happens to
invoke them.  All file I/O still goes through injected async callbacks so the
permission boundary (OpContext) is never bypassed — MemoryService knows
nothing about op_runtime, OpContext, Workspace, or PermissionResolver.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from reyn.core.events.events import EventLog

_INDEX_ENTRY_TEMPLATE = "- [{name}]({slug}.md) — {description}"
_INDEX_HEADER = "# Memory Index\n\n"


def strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block (``---\\n...\\n---\\n``) from
    a memory file's text and return the body alone.

    Used by :meth:`MemoryService.read_body` to give the LLM the actual
    remembered text instead of metadata fields it doesn't need (=
    ``name`` / ``description`` / ``type``). Returning the frontmatter intact
    triggered a G12 empty-stop attractor: the LLM sometimes parsed the
    metadata block as the content and exited with ``finish=stop`` /
    ``content=""`` instead of narrating the body (confirmed via dogfood
    trace on a ``who am I?`` recall).

    When the input doesn't start with a frontmatter delimiter the original
    text is returned unchanged — handles legacy memory files written before
    the frontmatter convention existed.
    """
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    # Find first non-blank line; require it to be exactly "---".
    lines = text.split("\n")
    # Skip leading blanks.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    # Find the closing "---" after the opening one.
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        # No closing delimiter — leave content alone rather than truncating.
        return text
    body_lines = lines[close + 1:]
    # Trim a single leading blank line that conventionally follows the
    # closing delimiter; keep subsequent whitespace as authored.
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


class MemoryKnowledgeSync:
    """Binds a memory write / delete to the workspace knowledge index.

    :class:`MemoryService` owns WHEN the knowledge index must follow a memory
    mutation (FP-0066 P3a, #3247 firm §3/§4); this collaborator owns WHICH
    coordinator and OpContext that ingest runs against, so the memory layer
    never has to hold an ``IndexCoordinator``, a ``workspace_root`` and an
    ``OpContext`` as three separate injected materials.

    Parameters
    ----------
    op_context_fn:
        Zero-arg callable returning a LIVE ``OpContext``. A callable, not a
        snapshot: an OpContext carries per-turn state (contextual permission,
        sandbox policy), so an eagerly-resolved one would be stale by the
        time a ``remember`` runs.
    events:
        The session's ``EventLog`` (forwarded to the ingest so its outcome
        records land on the session's audit trail).
    workspace_root_fn:
        Zero-arg callable returning the index's workspace root. Defaults to
        ``Path.cwd`` — which is what the previous router-loop call site
        resolved to in production: it read ``getattr(host, "workspace_root",
        None) or Path.cwd()`` against a ``RouterHostAdapter`` that has no
        ``workspace_root`` attribute at all, so the ``or`` arm was the live
        one. Preserved deliberately rather than "fixed" to the session's
        workspace base dir: the root is the index singleton's KEY, so
        changing it re-keys (and orphans) every existing memory index.
    """

    def __init__(
        self,
        *,
        op_context_fn: Callable[[], Any],
        events: "EventLog | None" = None,
        workspace_root_fn: "Callable[[], Path] | None" = None,
    ) -> None:
        self._op_context_fn = op_context_fn
        self._events = events
        self._workspace_root_fn = workspace_root_fn or Path.cwd

    def _coordinator(self) -> "tuple[Any, Path]":
        from reyn.data.index.coordinator import get_index_coordinator

        root = Path(self._workspace_root_fn())
        return get_index_coordinator(root), root

    async def ingest(self) -> None:
        """Re-embed the memory corpus (§G2 best-effort — never raises)."""
        from reyn.data.index.knowledge_ingest import sync_memory_ingest

        coordinator, root = self._coordinator()
        await sync_memory_ingest(
            coordinator, root, self._op_context_fn(), events=self._events,
        )

    async def deindex(self, layer: str, slug: str) -> None:
        """Drop one entry's embedded row (§G3 — RAISES on failure)."""
        from reyn.data.index.knowledge_ingest import sync_memory_deindex

        coordinator, _root = self._coordinator()
        await sync_memory_deindex(coordinator, layer, slug)


class MemoryService:
    """The memory-store capability: ``remember`` / ``forget`` / ``read_body``.

    Paths derive from ``agent_workspace_dir`` + the ``layer`` argument per
    call.  There is no mutable per-call state.

    Parameters
    ----------
    agent_workspace_dir:
        ``Path`` pointing to ``.reyn/agents/<agent_name>``.  Used to resolve
        the ``"agent"`` layer directory.
    events:
        The session's ``EventLog``.  Used to emit ``memory_saved``,
        ``memory_deleted`` and ``threat_block`` audit-events.
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
    threat_scan:
        ``ThreatScanConfig`` (or None = disabled) driving the FP-0050/#1822
        block scan applied to LLM-authored memory content before it persists.
    knowledge_sync:
        :class:`MemoryKnowledgeSync` (or None = no knowledge index) invoked
        after a successful write / delete.
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
        threat_scan: Any = None,
        knowledge_sync: Any = None,
    ) -> None:
        self._workspace = Path(agent_workspace_dir)
        self._events = events
        self._file_write = file_write
        self._file_read = file_read
        self._file_delete = file_delete
        self._file_regenerate_index = file_regenerate_index
        self._threat_scan = threat_scan
        self._knowledge_sync = knowledge_sync

    # ── Path helpers ─────────────────────────────────────────────────────────

    def memory_dir(self, layer: str) -> str:
        """Directory for the memory layer.

        layer="shared" → .reyn/memory
        layer="agent"  → .reyn/agents/<agent_name>/memory
        """
        if layer == "shared":
            # #3705: derived from `self._workspace` (already anchored on the
            # caller's real state root — `<state-root>/agents/<name>`) rather
            # than a bare relative `Path(".reyn")`, which silently ignored
            # it. `self._workspace.parent` = `<state-root>/agents`,
            # `.parent.parent` = `<state-root>` — the shared (non-agent-
            # scoped) memory dir sits directly under that.
            return str(self._workspace.parent.parent / "memory")
        return str(self._workspace / "memory")

    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer + slug to file path.

        layer="shared" → .reyn/memory/<slug>.md
        layer="agent"  → .reyn/agents/<agent_name>/memory/<slug>.md
        """
        return str(Path(self.memory_dir(layer)) / f"{slug}.md")

    # ── Domain rules ─────────────────────────────────────────────────────────

    def scan_for_block(self, content: str, *, scope: str = "strict"):
        """FP-0050/#1822 S4a (Class B): return the first block-severity threat
        match in ``content`` at ``scope``, or None.

        Applied at the agent-write seam (``remember``) to REJECT a poisoned
        write before it persists — a poisoned entry would re-enter the system
        prompt every session, so it is blocked rather than fenced. Emits a
        ``threat_block`` audit-event on a hit. No-op (None) when disabled;
        fail-open.
        """
        cfg = self._threat_scan
        if cfg is None or not getattr(cfg, "enabled", True):  # #4523: shadow default matches ThreatScanConfig.enabled's own declared True
            return None
        from reyn.security.content_guard import first_blocking_match, scan_for_threats
        try:
            matches = scan_for_threats(content, cfg, scope=scope)
        except Exception:  # noqa: BLE001 — fail-open
            if getattr(cfg, "fail_open", True):
                return None
            raise
        hit = first_blocking_match(matches, getattr(cfg, "block_severity", "block"))
        if hit is not None:
            self._events.emit(
                "threat_block", pattern_id=hit.pattern_id, severity=hit.severity,
                scope=hit.scope,
            )
        return hit

    async def _regenerate_listing_index(self, layer: str) -> dict:
        mem_dir = self.memory_dir(layer)
        return await self._file_regenerate_index(
            path=mem_dir,
            output_path=str(Path(mem_dir) / "MEMORY.md"),
            entry_template=_INDEX_ENTRY_TEMPLATE,
            header=_INDEX_HEADER,
        )

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
        """Persist a memory entry and refresh both indexes.

        Scans the LLM-authored content for a blocking threat match and
        REJECTS the write on a hit; otherwise constructs YAML frontmatter,
        writes the body file, regenerates the layer's ``MEMORY.md`` listing
        index, then re-embeds the memory corpus into the knowledge index.

        Returns ``{"saved": slug, "layer": layer}``, or an error result — the
        threat block returns the decision-enabling ``{"status": "error",
        "error": {...}}`` deny envelope (what matched + how to fix, nothing
        persisted); a file failure returns ``{"error": <reason>}``.
        """
        hit = self.scan_for_block(f"{name}\n{description}\n{body}", scope="strict")
        if hit is not None:
            return {
                "status": "error",
                "error": {
                    "kind": "threat_blocked",
                    "message": (
                        f"memory write blocked: content matched threat pattern "
                        f"'{hit.pattern_id}' ({hit.scope}/{hit.severity}). Remove the "
                        f"flagged content (injection / exfiltration / config-mod phrasing) "
                        f"from the entry and retry."
                    ),
                    "pattern_id": hit.pattern_id,
                },
            }

        # Defensive: strip trailing .md if the LLM emitted it in slug despite
        # the tool description saying "Filename stem".
        if slug.endswith(".md"):
            slug = slug[:-3]

        # memory_path appends .md itself — pass the bare slug.
        body_path = self.memory_path(layer, slug)
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {type}\n---\n\n{body}\n"
        )
        write_result = await self._file_write(body_path, frontmatter)
        if "error" in write_result:
            return {"error": write_result["error"]}

        regen_result = await self._regenerate_listing_index(layer)
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._events.emit("memory_saved", layer=layer, slug=slug, path=body_path)

        # FP-0066 P3a (#3247 firm §3/§7): dynamic sync-in-op knowledge ingest —
        # after the write + listing-index regen both succeed, (re)embed the
        # memory corpus. Best-effort (§G2): a provider/embedding failure must
        # not fail this `remember` — see ``sync_memory_ingest``'s docstring.
        if self._knowledge_sync is not None:
            await self._knowledge_sync.ingest()
        return {"saved": slug, "layer": layer}

    async def forget(self, *, layer: str, slug: str) -> dict:
        """Delete a memory entry and refresh both indexes.

        Returns ``{"deleted": slug, "layer": layer}`` or ``{"error":
        <reason>}`` if the entry was not found or the knowledge de-index
        failed.
        """
        # Defensive: strip trailing .md if the LLM emitted it.
        if slug.endswith(".md"):
            slug = slug[:-3]
        body_path = self.memory_path(layer, slug)
        del_result = await self._file_delete(body_path)
        if "error" in del_result:
            return {"error": del_result["error"]}
        if not del_result.get("deleted"):
            return {"error": f"memory entry not found: {slug}"}

        regen_result = await self._regenerate_listing_index(layer)
        if "error" in regen_result:
            return {"error": regen_result["error"]}

        self._events.emit("memory_deleted", layer=layer, slug=slug, path=body_path)

        # FP-0066 P3a (#3247 firm §4 G3): sync de-index — NOT best-effort. A
        # stale embedded row for a just-forgotten entry would be discoverable
        # by a future search over content that no longer exists, so a failure
        # here is surfaced to the caller rather than swallowed.
        if self._knowledge_sync is not None:
            try:
                await self._knowledge_sync.deindex(layer, slug)
            except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
                return {
                    "error": f"knowledge de-index failed: {exc}",
                    "layer": layer, "slug": slug,
                }
        return {"deleted": slug, "layer": layer}

    async def read_body(
        self,
        *,
        layer: str,
        slug: str,
        offset: "int | None" = None,
        limit: "int | None" = None,
    ) -> dict:
        """Read a memory entry's body, frontmatter stripped.

        Returns ``{"layer": layer, "slug": slug, "content": <text>}`` —
        plus, when the underlying read was truncated, whichever #3193
        signal fields ``_file_read`` forwarded (``truncated``, ``note``,
        ``next_offset``, ...), copied through untouched — or
        ``{"error": <reason>, "layer": layer, "slug": slug}`` if not found.

        #3193: this used to hand-pick only ``content`` off the ``_file_read``
        result, which silently dropped any truncation signal even after
        ``_file_read`` itself started forwarding it — a large memory body
        would read back with content cut short and NO indication anything
        was cut. Forward every extra key ``_file_read`` returns (beyond the
        ones this method already names) instead of re-curating a subset.

        Optional ``offset`` / ``limit`` line-slice applies AFTER the
        frontmatter is stripped, so the offset counts the content the LLM
        would actually see.
        """
        body_path = self.memory_path(layer, slug)
        result = await self._file_read(body_path)
        if "error" in result:
            return {"error": result["error"], "layer": layer, "slug": slug}
        body = strip_frontmatter(result["content"])
        if offset is not None or limit is not None:
            lines = body.splitlines(keepends=True)
            start = max(0, offset or 0)
            sliced = (
                lines[start:start + limit] if limit is not None
                else lines[start:]
            )
            body = "".join(sliced)
        out = {"layer": layer, "slug": slug, "content": body}
        for key, value in result.items():
            if key not in ("path", "content"):
                out[key] = value
        return out


__all__ = ["MemoryKnowledgeSync", "MemoryService", "strip_frontmatter"]
