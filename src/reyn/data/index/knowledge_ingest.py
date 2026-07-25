"""Knowledge-ingest builders for the IndexCoordinator (FP-0066 P3a, #3247
"P3 設計 firm" — the arc's first real production caller of ``mark_dirty``/
``register_builder``/``ensure_built``).

**Why this module exists (avoid dispersion, per the Coordinator's own
boundary principle)**: memory (``remember``/``forget_memory``) has TWO
call paths that both need the same sync-in-op ingest/de-index behavior —
the production ``RouterLoop._remember``/``_forget`` path
(``src/reyn/runtime/router_loop.py``) and the non-router fallback path in
``src/reyn/tools/memory.py`` (used by phase/test callers without a live
router). Skill install/uninstall similarly has one production entry point
each (``skill_install`` op / ``plugin_uninstall`` op). Putting the
domain-adapter (``BuildFn``) + the "mark dirty, then sync-await" glue in
ONE shared module — rather than duplicating it at each call site — is
exactly what the Coordinator module docstring warns against: the
``file.read`` skill-special-casing dispersion the P2 firm named as the
failure mode centralised orchestration exists to prevent.

**Two dynamic sources registered here** (FP-0066 P3a §7(b) loud-kind):
``KNOWLEDGE_MEMORY_SOURCE_ID`` (all memory entries, shared + agent layers,
one shared source rebuilt in full on every sync-in-op call — mirrors the
existing action-catalog's full-rebuild-per-build shape, not an incremental
diff) and ``KNOWLEDGE_SKILL_SOURCE_ID`` (all registered skills' ``SKILL.md``
bodies, one shared source). Both register with ``kind="dynamic"`` (§3 sync-
in-op rule: operator-driven op, must not be a foreground surprise-free
"static" background build).

**§G2 best-effort ingest**: ``sync_memory_ingest``/``sync_skill_ingest``
never raise — a provider failure (or embedding disabled, or any other
build fault) leaves the source ``dirty`` (``IndexCoordinator.ensure_built``
already catches build failures internally; this module's wrapper
additionally swallows any exception from ``register_builder``/
``mark_dirty`` themselves, which are cheap local calls not expected to
fail but must not be allowed to fail the `remember`/`skill_install` op if
they somehow do).

**§G3 sync de-index — NOT best-effort**: ``sync_memory_deindex``/
``sync_skill_deindex`` DO raise on failure. A forgotten memory entry or an
uninstalled plugin's skill leaving a stale (searchable) vector row behind
is the exact "best-effort search is a bug" failure the firm's §4 calls out
— the caller (``forget_memory``/``plugin_uninstall``) is expected to
catch and surface the failure as its own error shape, not silently
swallow it.

Per-entry deletion uses ``IndexCoordinator.delete_entries`` (new in this
PR — see its docstring in ``coordinator.py`` for why: the task brief's
"via index_drop" is not mechanically right for entry-level de-index —
``index_drop`` is a documented WHOLE-SOURCE drop with no per-item
selection; ``IndexBackend.delete(source, content_hashes)`` is the existing
primitive that supports exactly this, and ``delete_entries`` is a thin
Coordinator-level wrapper around it so the call routes through the same
cross-process build lock a concurrent build/ingest uses, rather than
reaching for the raw backend directly from a tool/op handler).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.data.index.backend import ChunkRecord
from reyn.data.index.coordinator import BuildFn, BuildMaterial, IndexCoordinator
from reyn.data.skills.registry import SkillEntry, build_skill_registry

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext

__all__ = [
    "KNOWLEDGE_MEMORY_SOURCE_ID",
    "KNOWLEDGE_SKILL_SOURCE_ID",
    "memory_content_hash",
    "skill_content_hash",
    "resolve_default_embedding_model_class",
    "sync_memory_ingest",
    "sync_memory_deindex",
    "sync_skill_ingest",
    "sync_skill_deindex",
]

# Distinct from the bare "memory" name a user could hand ``index_docs`` for
# a user-defined doc-RAG source (``sources.yaml`` documents that example
# literally) — a bare "memory" source_id here would silently collide with
# an operator-created source of the same name on the same manifest/backend.
KNOWLEDGE_MEMORY_SOURCE_ID = "knowledge_memory"
KNOWLEDGE_SKILL_SOURCE_ID = "knowledge_skill"


def resolve_default_embedding_model_class() -> str:
    """Resolve ``embedding.default_class`` off the live effective config.

    Mirrors ``op_runtime.embed._resolve_provider``'s own ``load_config()``
    pattern — a fresh load per call (op handlers/tool handlers are
    stateless). Falls back to ``"standard"`` (the dataclass default) on any
    config-load failure; the actual embed call still fails closed via the
    ``embedding.enabled`` gate regardless of which model-class string is
    used to ask for it.
    """
    try:
        from reyn.config import load_config
        return load_config().embedding.default_class
    except Exception:
        return "standard"


def memory_content_hash(layer: str, slug: str) -> str:
    """Deterministic per-entry identifier for a memory entry's embedded
    chunk row — stable across rebuilds so a re-embed of unchanged content
    dedups (backend's ``content_hash`` UNIQUE constraint) and so
    ``sync_memory_deindex`` can address exactly this one row."""
    return hashlib.sha256(f"memory:{layer}:{slug}".encode("utf-8")).hexdigest()


def skill_content_hash(name: str) -> str:
    """Deterministic per-entry identifier for a skill's embedded chunk row
    (see ``memory_content_hash``)."""
    return hashlib.sha256(f"skill:{name}".encode("utf-8")).hexdigest()


# ── memory ingest (dynamic, sync-in-op) ────────────────────────────────────


def _strip_frontmatter(content: str) -> str:
    """Same logic as ``reyn.tools.memory._strip_frontmatter`` (ported
    rather than imported: that function is private to the tools-layer
    handler module and this module lives one layer below it in the
    dependency graph — ``reyn.tools.memory`` will come to depend on THIS
    module, not the other way around, so importing back up would invert
    the layering). Kept byte-identical to avoid a second interpretation
    of what "the body text" means for embedding vs. for the read tool."""
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        return text
    body_lines = lines[close + 1:]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


def _iter_memory_entries(workspace_root: Path) -> list[tuple[str, str, str]]:
    """Enumerate ``(layer, slug, body_text)`` across BOTH memory layers.

    Mirrors ``reyn.tools.memory._memory_dir``'s directory layout
    (``<state_dir>/memory`` for shared, ``<state_dir>/agents/memory`` for
    agent) without needing a live ``ToolContext`` — this builder runs
    later, inside a Coordinator build, potentially with a different
    ``OpContext`` than the one that triggered the ingest.
    """
    state_dir = workspace_root / ".reyn"
    layer_dirs = (
        ("shared", state_dir / "memory"),
        ("agent", state_dir / "agents" / "memory"),
    )
    out: list[tuple[str, str, str]] = []
    for layer, mem_dir in layer_dirs:
        if not mem_dir.is_dir():
            continue
        for path in sorted(mem_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            out.append((layer, path.stem, _strip_frontmatter(raw)))
    return out


def _memory_to_chunk_record(
    item: tuple[str, str, str], vector: list[float], resolved_model: str,
) -> ChunkRecord:
    layer, slug, text = item
    return ChunkRecord(
        text=text,
        vector=list(vector),
        metadata={
            "source_path": f"{layer}/{slug}.md",
            "source_type": "memory",
            "content_hash": memory_content_hash(layer, slug),
            "embedding_model": resolved_model,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": None,
            "extra": {"layer": layer, "slug": slug},
        },
        score=None,
    )


def _memory_build_fn(workspace_root: Path, op_ctx: "OpContext", model_class: str) -> BuildFn:
    async def _build() -> BuildMaterial:
        entries = _iter_memory_entries(workspace_root)
        return BuildMaterial(
            items=list(entries),
            texts=[text for (_layer, _slug, text) in entries],
            to_chunk_record=_memory_to_chunk_record,
            model_class=model_class,
            ctx=op_ctx,
        )
    return _build


async def sync_memory_ingest(
    coordinator: IndexCoordinator,
    workspace_root: Path,
    op_ctx: "OpContext",
    *,
    events: "EventLog | None" = None,
) -> None:
    """Sync-in-op memory embedding ingest (§G2 best-effort — never raises).

    Called AFTER a ``remember`` write (+ listing-index regen) has already
    succeeded. Re-registers the memory builder (idempotent — see
    ``register_builder``'s docstring; a fresh closure captures the CURRENT
    ``op_ctx``, since the previously-registered one may be stale), marks
    the shared memory source dirty (the just-written entry invalidates any
    prior clean build), then awaits a synchronous rebuild.

    Never raises: ``ensure_built`` itself already never raises (a build
    failure is caught, mark_dirty'd, and reported on the ``BuildOutcome``,
    per the Coordinator's own §G2 contract) — the ``try/except`` here only
    guards against a failure in the (cheap, local) registration/mark_dirty
    calls themselves, so a `remember` call can NEVER fail because of this
    ingest hook.
    """
    try:
        model_class = resolve_default_embedding_model_class()
        coordinator.register_builder(
            KNOWLEDGE_MEMORY_SOURCE_ID,
            _memory_build_fn(workspace_root, op_ctx, model_class),
            kind="dynamic",
        )
        await coordinator.mark_dirty(KNOWLEDGE_MEMORY_SOURCE_ID, reason="memory_write")
        await coordinator.ensure_built(
            KNOWLEDGE_MEMORY_SOURCE_ID, await_completion=True, events=events,
        )
    except Exception:
        pass


async def sync_memory_deindex(
    coordinator: IndexCoordinator, layer: str, slug: str,
) -> None:
    """Sync per-entry de-index (§G3 — NOT best-effort, raises on failure).

    Called AFTER a ``forget_memory`` file-delete (+ listing-index regen)
    has already succeeded. Removes exactly this entry's embedded chunk row
    — a stale row left behind would be discoverable by a future
    ``search_knowledge`` (P3c) for content that no longer exists, the
    canonical "best-effort search is a bug" failure the firm's §4 names.
    """
    await coordinator.delete_entries(
        KNOWLEDGE_MEMORY_SOURCE_ID, [memory_content_hash(layer, slug)],
    )


# ── skill ingest (dynamic, sync-in-op) ─────────────────────────────────────


def _skill_to_chunk_record(
    item: SkillEntry, vector: list[float], resolved_model: str,
) -> ChunkRecord:
    return ChunkRecord(
        text=item.description,
        vector=list(vector),
        metadata={
            "source_path": item.path,
            "source_type": "skill",
            "content_hash": skill_content_hash(item.name),
            "embedding_model": resolved_model,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": None,
            "extra": {"name": item.name},
        },
        score=None,
    )


def _read_skill_body(path_str: str) -> str:
    """Read a skill's ``SKILL.md`` body (directory or direct-file path, same
    resolution rule as ``op_runtime.skill_install._resolve_skill_md``).
    Missing/unreadable content degrades to ``""`` (the item is still
    embedded — a skill's name/description alone, from ``_entry_from_config``'s
    truncation, still carries meaningful discovery text) rather than
    dropping the skill from the ingest batch entirely."""
    p = Path(path_str)
    skill_md = p / "SKILL.md" if p.is_dir() else p
    try:
        return skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""


def _skill_build_fn(raw_skills: dict[str, Any], op_ctx: "OpContext", model_class: str) -> BuildFn:
    async def _build() -> BuildMaterial:
        entries = build_skill_registry(raw_skills)
        texts = [
            (entry.description + "\n\n" + _read_skill_body(entry.path)).strip()
            for entry in entries
        ]
        return BuildMaterial(
            items=list(entries),
            texts=texts,
            to_chunk_record=_skill_to_chunk_record,
            model_class=model_class,
            ctx=op_ctx,
        )
    return _build


async def sync_skill_ingest(
    coordinator: IndexCoordinator,
    raw_skills: dict[str, Any],
    op_ctx: "OpContext",
    *,
    events: "EventLog | None" = None,
) -> None:
    """Sync-in-op skill embedding ingest (§G2 best-effort — never raises).

    Called AFTER ``skill_install`` has already durably written the new
    entry to ``skills.yaml`` (+ recorded its config generation). ``raw_skills``
    is the SAME raw ``skills.entries`` dict ``build_skill_registry`` already
    consumes elsewhere (``factory_config.py``, ``session.py``) — read fresh
    by the caller (a just-installed entry must be visible), not re-loaded
    here, to avoid a second, possibly-stale config load.
    """
    try:
        model_class = resolve_default_embedding_model_class()
        coordinator.register_builder(
            KNOWLEDGE_SKILL_SOURCE_ID,
            _skill_build_fn(raw_skills, op_ctx, model_class),
            kind="dynamic",
        )
        await coordinator.mark_dirty(KNOWLEDGE_SKILL_SOURCE_ID, reason="skill_install")
        await coordinator.ensure_built(
            KNOWLEDGE_SKILL_SOURCE_ID, await_completion=True, events=events,
        )
    except Exception:
        pass


async def sync_skill_deindex(coordinator: IndexCoordinator, names: list[str]) -> None:
    """Sync de-index of every skill named in ``names`` (§G3 — NOT
    best-effort, raises on failure).

    Called AFTER ``plugin_uninstall`` has already durably dropped these
    entries from ``skills.yaml`` (the firm's §4 "plugin-unit de-index"
    ruling — a plugin's ENTIRE set of skills is removed together, one
    ``delete_entries`` call per uninstall covering however many skill
    names that plugin had registered; builtin skills are never in
    ``names`` here since they have no uninstall path — see the firm's §4
    "builtin = immutable premise" ruling)."""
    if not names:
        return
    await coordinator.delete_entries(
        KNOWLEDGE_SKILL_SOURCE_ID, [skill_content_hash(name) for name in names],
    )
