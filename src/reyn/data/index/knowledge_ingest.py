"""Knowledge-ingest builders for the IndexCoordinator (FP-0066 P3a, #3247
"P3 設計 firm" — the arc's first real production caller of ``mark_dirty``/
``register_builder``/``ensure_built``).

**Why this module exists (avoid dispersion, per the Coordinator's own
boundary principle)**: memory (``remember``/``forget_memory``) has TWO
call paths that both need the same sync-in-op ingest/de-index behavior —
the production ``MemoryService.remember``/``forget`` path
(``src/reyn/runtime/services/memory_service.py``, reached through the
``MemoryKnowledgeSync`` collaborator that binds these functions to a live
coordinator + OpContext) and the non-router fallback path in
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

**FP-0066 P3b (#3247 "P3 設計 firm" §1/§3/§4) — repo_doc/repo_src added**:
a THIRD ingest pair, distinct in shape from memory/skill in two ways per
the firm's ruling:

  1. **kind="static", mode="background"** (firm §3 sync-vs-bg table): the
     repo corpus is not an operator-direct-action producer like a
     ``remember``/``skill_install`` call — it changes only when the Reyn
     install itself changes. The trigger is therefore "enable-time
     background backfill" (``ensure_built(await_completion=False)`` →
     ``asyncio.create_task``), NEVER a sync-in-op await — see
     ``sync_repo_ingest_background`` below and its production wiring in
     ``RouterLoop`` (the same per-turn slot the action-catalog's own
     background build already occupies, itself already background/
     never-block per the same §3 rule).
  2. **§G3 = source-unit de-index, not per-entry** (firm §4's explicit
     ruling on the census's ⚠️ finding that repo has no per-file delete
     trigger): unlike ``sync_memory_deindex``/``sync_skill_deindex``,
     there is NO ``sync_repo_deindex`` here. The existing ``index_drop``
     op (``core/op_runtime/index_drop.py`` — whole-source
     ``backend.drop(source)`` + ``manifest.remove(source)``, already
     live per FP-0066 P1b) IS the repo de-index mechanism as-is: calling
     it with ``op.source = KNOWLEDGE_REPO_DOC_SOURCE_ID`` (or
     ``..._SRC_...``) removes every entity that source's build wrote, in
     one whole-source operation — exactly the granularity the firm
     names as sufficient for v1 (a repo source is operator-dropped as a
     unit; per-file staleness is covered by the next build's full-
     replace reconcile, not a live delete trigger). Per-file de-index is
     an explicit FUTURE ticket (firm §12), not built here.

**doc/src classification (v1 = extension-based, per the brief)**:
``_classify_repo_kind`` returns ``"doc"`` for a ``.md`` file (covers
``README.md``, ``CHANGELOG.md``, and every ``docs/**/*.md`` — including
``*.ja.md``, since that is still a ``.md`` suffix) and ``"src"`` for
everything else under the reachable set that decodes as UTF-8 text
(overwhelmingly ``src/**/*.py``). A file that is neither (binary, or
fails UTF-8 decode) is skipped, not misclassified into either bucket.
Code-AWARE chunking for ``repo_src`` (AST-based / symbol-boundary
splitting) is explicitly a FUTURE ticket (firm §12) — v1 embeds each
file as ONE chunk (``chunk_index=0``), the same plain-text-chunk shape
``knowledge_memory``/``knowledge_skill`` already use, not a special case.

**Content-hash reconcile without a live watcher**: each background build
re-enumerates the repo from disk and calls ``embed_verify_write`` with
``mode="replace"`` — the SAME full-rebuild-per-build shape memory/skill
use (see their docstrings above). A changed file's TEXT differs, so its
next build's embedded chunk reflects the new content; a removed file is
simply absent from the enumerated ``items`` this build, so
``mode="replace"``'s ``DELETE FROM chunks`` (before the fresh insert)
drops its row with it — "add" / "update" / "remove" are all handled by
the same full-replace-per-build primitive already in ``sqlite.py``, with
no separate live filesystem watcher needed (explicitly out of scope per
the brief — the next background build's reconcile is sufficient for v1).
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
    "KNOWLEDGE_REPO_DOC_SOURCE_ID",
    "KNOWLEDGE_REPO_SRC_SOURCE_ID",
    "memory_content_hash",
    "skill_content_hash",
    "repo_content_hash",
    "resolve_default_embedding_model_class",
    "sync_memory_ingest",
    "sync_memory_deindex",
    "sync_skill_ingest",
    "sync_skill_deindex",
    "sync_repo_ingest_background",
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


# ── repo ingest (static, background) — FP-0066 P3b ─────────────────────────

KNOWLEDGE_REPO_DOC_SOURCE_ID = "knowledge_repo_doc"
KNOWLEDGE_REPO_SRC_SOURCE_ID = "knowledge_repo_src"

# Read cap mirrors ``reyn_repo_read``'s own ``_MAX_READ_BYTES`` (256 KB) —
# a large generated/binary-ish file under the reachable set should not
# blow up a single embed batch. A file over this cap is skipped for
# ingest (still readable one-off via ``reyn_repo_read``'s own offset/
# limit slicing — this cap is an ingest-batch concern, not a read-tool one).
_REPO_INGEST_MAX_BYTES = 256 * 1024


def repo_content_hash(kind: str, rel_path: str) -> str:
    """Deterministic per-entity identifier for a repo file's embedded chunk
    row (see ``memory_content_hash``/``skill_content_hash``). Identity is
    ``(kind, rel_path)`` — NOT content — matching the same convention: the
    hash addresses "this file", and ``mode="replace"`` (full source
    rebuild each background build) is what keeps the row's TEXT current
    with the file's latest content, not a change in the hash itself."""
    return hashlib.sha256(f"repo_{kind}:{rel_path}".encode("utf-8")).hexdigest()


def _classify_repo_kind(rel_path: str) -> "str | None":
    """v1 doc/src classification (per the brief — extension-based is fine
    for v1): ``.md`` (covers ``.ja.md`` too, same suffix) -> ``"doc"``;
    anything else reachable -> ``"src"``. Never returns anything but
    ``"doc"``/``"src"``/``None`` — ``None`` is not a third kind, it means
    "this path is not classified" and the caller skips it (used for the
    handful of non-text assets under ``docs/`` if any exist, e.g. images)."""
    if rel_path.endswith(".md"):
        return "doc"
    return "src"


def _iter_repo_entries(wanted_kind: str) -> "tuple[list[tuple[str, str, str]], int]":
    """Enumerate ``(kind, rel_path, text)`` for every reachable repo file
    classified as ``wanted_kind`` ("doc" or "src"). Returns ``(entries,
    skipped_for_size_count)``.

    Walks ``reyn.runtime.reyn_repo``'s OWN reachable-set root
    (``resolve_reyn_root()`` + ``REACHABLE_TOP_LEVEL_ENTRIES`` — the same
    ``{README.md, CHANGELOG.md, docs, src}`` scope ``reyn_repo_list``/
    ``reyn_repo_read``/``reyn_repo_glob`` already present to the LLM, so
    this ingest never reaches a path the read tools themselves would
    refuse) directly via ``pathlib``, rather than importing
    ``reyn_repo``'s private ``_iter_files_under``/``_SKIP_DIR_NAMES``
    helpers across the runtime/data layer boundary (mirrors this module's
    existing "port, don't import-up" convention — see
    ``_strip_frontmatter``'s docstring above for the same layering
    rationale: ``reyn.runtime`` will come to depend on ``reyn.data.index``
    conceptually [repo ingest feeds the same embedding substrate the
    runtime's ``search_actions`` already consumes], not the reverse).

    A file that fails UTF-8 decode, or sits under a noise directory
    (``.git``, ``__pycache__``, etc. — same skip-set ``reyn_repo`` itself
    applies), is skipped without being counted — a decode failure or a VCS/
    build-artifact path is not a corpus gap, it was never eligible content.
    A file that exceeds ``_REPO_INGEST_MAX_BYTES`` IS counted (#4431 — was
    silently dropped with no signal at all; ``_repo_build_fn`` emits a
    ``repo_ingest_files_skipped`` audit-event off this count when it's
    nonzero, so a file missing from repo-knowledge search has a trail
    instead of reading as "was never written").
    """
    from reyn.runtime.reyn_repo import REACHABLE_TOP_LEVEL_ENTRIES, resolve_reyn_root

    skip_dir_names = frozenset({
        ".git", ".reyn", ".github", ".claude", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "__pycache__", "venv", ".venv",
        "site", "build", "dist", "node_modules",
    })
    try:
        root = resolve_reyn_root()
    except RuntimeError:
        # No dev repo / no wheel _bundled dir resolvable in this
        # environment (e.g. a stripped-down test install) — an empty
        # corpus, not an ingest failure; ``embed_verify_write`` handles
        # zero items/texts fine (writes an empty batch).
        return [], 0

    out: list[tuple[str, str, str]] = []
    skipped_for_size = 0
    for top in REACHABLE_TOP_LEVEL_ENTRIES:
        top_path = root / top
        if not top_path.exists():
            continue
        candidates = [top_path] if top_path.is_file() else sorted(top_path.rglob("*"))
        for p in candidates:
            if not p.is_file():
                continue
            try:
                rel_parts = p.relative_to(root).parts
            except ValueError:
                continue
            if any(part in skip_dir_names for part in rel_parts):
                continue
            rel_path = "/".join(rel_parts)
            kind = _classify_repo_kind(rel_path)
            if kind != wanted_kind:
                continue
            try:
                if p.stat().st_size > _REPO_INGEST_MAX_BYTES:
                    skipped_for_size += 1
                    continue
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.append((kind, rel_path, text))
    return out, skipped_for_size


def _repo_to_chunk_record(
    item: tuple[str, str, str], vector: list[float], resolved_model: str,
) -> ChunkRecord:
    kind, rel_path, text = item
    return ChunkRecord(
        text=text,
        vector=list(vector),
        metadata={
            "source_path": rel_path,
            "source_type": f"repo_{kind}",
            "content_hash": repo_content_hash(kind, rel_path),
            "embedding_model": resolved_model,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": None,
            "extra": {"kind": kind},
        },
        score=None,
    )


def _repo_build_fn(wanted_kind: str, op_ctx: "OpContext", model_class: str) -> BuildFn:
    async def _build() -> BuildMaterial:
        entries, skipped_for_size = _iter_repo_entries(wanted_kind)
        if skipped_for_size:
            # #4431: the visibility half of the size cap — a file that lost
            # this way otherwise had no trail at all (see
            # `_iter_repo_entries`'s docstring). Best-effort, matches every
            # other op_ctx.events.emit call site in op_runtime — a broken/
            # absent sink must not fail the build.
            try:
                op_ctx.events.emit(
                    "repo_ingest_files_skipped",
                    kind=wanted_kind, skipped_count=skipped_for_size,
                    reason="over_size_cap",
                )
            except Exception:
                pass
        return BuildMaterial(
            items=list(entries),
            texts=[text for (_kind, _rel_path, text) in entries],
            to_chunk_record=_repo_to_chunk_record,
            model_class=model_class,
            ctx=op_ctx,
        )
    return _build


def _embedding_enabled() -> bool:
    """Same ``embedding.enabled`` gate ``core.op_runtime.embed`` itself
    self-checks (ported, not imported — that function is private to the
    op_runtime module) used here PURELY as a scheduling short-circuit: if
    embedding is off, skip scheduling the background build entirely rather
    than spawning a task every turn that would fail the same way each time
    (enumerate → embed op raises `embedding disabled` → caught, mark_dirty,
    repeat next turn). ``embed_verify_write`` itself still fails closed
    regardless — this is a scheduling-noise optimization, not the gate."""
    try:
        from reyn.config import load_config
        return bool(load_config().embedding.enabled)
    except Exception:
        return False


async def sync_repo_ingest_background(
    coordinator: IndexCoordinator,
    op_ctx: "OpContext",
    *,
    events: "EventLog | None" = None,
) -> None:
    """Static/background repo_doc + repo_src ingest (firm §3: repo is
    "static", trigger = enable-time background backfill, NEVER sync-in-op).

    Registers both builders (idempotent, ``kind="static"``) then calls
    ``ensure_built(source_id, await_completion=False)`` for each — the
    background branch ONLY schedules an ``asyncio.create_task`` and
    returns immediately; it does not await the build itself. Safe to call
    on every router-loop turn (mirrors the action-catalog's own per-turn
    ``_ensure_action_index_built`` re-check): a ``clean`` source is a
    cheap manifest-read no-op, a ``building`` source returns immediately
    without re-spawning (once-per-source dedup lives in the Coordinator),
    and a fresh/dirty source spawns (or re-spawns, on a prior failure) the
    background build.

    Never raises (mirrors the ``sync_*_ingest`` §G2 best-effort contract
    even though these are "static", not "dynamic" — an unaware caller,
    the router-loop turn preamble, must never fail because of this
    scheduling call). No-ops entirely (does not even register/ensure_built)
    when ``embedding.enabled`` is false — see ``_embedding_enabled``.
    """
    if not _embedding_enabled():
        return
    try:
        model_class = resolve_default_embedding_model_class()
        coordinator.register_builder(
            KNOWLEDGE_REPO_DOC_SOURCE_ID,
            _repo_build_fn("doc", op_ctx, model_class),
            kind="static",
        )
        coordinator.register_builder(
            KNOWLEDGE_REPO_SRC_SOURCE_ID,
            _repo_build_fn("src", op_ctx, model_class),
            kind="static",
        )
        await coordinator.ensure_built(
            KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=False, events=events,
        )
        await coordinator.ensure_built(
            KNOWLEDGE_REPO_SRC_SOURCE_ID, await_completion=False, events=events,
        )
    except Exception:
        pass
