"""``reyn embeddings`` — inspect and manage the action embedding index.

FP-0043 Component C.2. Provides an operator surface for the embedding
machinery that backs ``search_actions``:

  status   table or JSON dump of configured embedding classes + the
           on-disk action index state
  rebuild  force a rebuild of the action index on next chat session
           start (= removes the SQLite cache so the next ``build()``
           call re-embeds)
  clear    wipe the cache directory entirely (= SQLite index, build
           lock, downloaded sentence-transformers model cache)

The shape mirrors ``reyn mcp list`` (= header + aligned rows) per the
tui-coder + lead-coder consolidated O4 decision in FP-0043. ``--json``
is supported on every subcommand for scripting / N=20-bench data
aggregation pipelines.

Network-touching ops (= would-be probes against the embedding API)
are NOT performed by this command — like ``mcp list`` without
``--probe``, it reads on-disk state only. Network behaviour is
verified by the bench runner (``scripts/embedding_bench.py``).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reyn.config import load_config

# ── data shape ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassRow:
    """One row in the ``status`` output."""

    name: str
    backend: str          # "litellm" / "sentence-transformers"
    model: str            # resolved model string (= class.model)
    cache_path: str       # filesystem path or "(memory only)"
    size_mb: float        # cached SQLite + model bytes, 0.0 when absent
    indexed_actions: int  # vectors row count, 0 when absent / unreadable
    last_built: str       # ISO timestamp or "(never)"


# ── path resolution ───────────────────────────────────────────────────────────


def _resolve_action_index_dir(project_root: Path) -> Path:
    """Return the directory the action index SQLite lives in.

    Mirrors the convention RouterLoop uses: ``<project>/.reyn/action_index/``.
    The presence of the directory is the source of truth for "has been
    built at least once"; the absence is a clean state.
    """
    return project_root / ".reyn" / "action_index"


def _resolve_st_cache_dir() -> Path:
    """Return the sentence-transformers model cache dir.

    Honours the same precedence the runtime backend uses:
      REYN_CACHE_DIR > XDG_CACHE_HOME > ~/.cache/reyn/
    See ``src/reyn/embedding/sentence_transformers_provider.py``
    for the canonical implementation; we duplicate the resolution
    here (rather than importing it) to avoid a transitive
    ``sentence_transformers`` import the CLI doesn't need.
    """
    if v := os.environ.get("REYN_CACHE_DIR"):
        root = Path(v).expanduser()
    elif v := os.environ.get("XDG_CACHE_HOME"):
        root = Path(v).expanduser() / "reyn"
    else:
        root = Path.home() / ".cache" / "reyn"
    return root / "sentence-transformers"


def _get_project_root() -> Path:
    """Return cwd as the project root (consistent with other reyn commands)."""
    return Path.cwd()


# ── status data collection ────────────────────────────────────────────────────


def _dir_size_mb(path: Path) -> float:
    """Return the total bytes under ``path`` as MB. 0.0 if absent."""
    if not path.exists():
        return 0.0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return round(total / (1024 * 1024), 2)


def _read_index_state(index_dir: Path) -> tuple[int, str]:
    """Return ``(indexed_actions, last_built_iso)`` from the SQLite cache.

    ``indexed_actions`` is the row count of the ``vectors`` table;
    ``last_built_iso`` is the file mtime of ``index.db`` formatted as
    an ISO 8601 string. Both default to (0, "(never)") when the file
    is absent or unreadable.
    """
    db_path = index_dir / "index.db"
    if not db_path.exists():
        return 0, "(never)"
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM vectors"
            ).fetchone()
            n = int(row[0]) if row else 0
        finally:
            con.close()
    except (sqlite3.DatabaseError, OSError):
        n = 0
    try:
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(
            db_path.stat().st_mtime,
            tz=_dt.timezone.utc,
        )
        last_built = ts.replace(microsecond=0).isoformat()
    except OSError:
        last_built = "(never)"
    return n, last_built


def _backend_for_model(model: str) -> str:
    """Return ``litellm`` or ``sentence-transformers`` based on prefix."""
    if model.startswith("sentence-transformers/"):
        return "sentence-transformers"
    return "litellm"


def _collect_status_rows(project_root: Path) -> list[ClassRow]:
    """Build one ``ClassRow`` per configured embedding class.

    The on-disk index state (= ``indexed_actions`` / ``last_built``)
    is shared across classes — the SQLite cache stores one ``model_class``
    at a time per FP-0043 Component E. We attribute the state to the
    class currently recorded in ``meta.model_class``; other classes
    get the zero/never defaults so the operator can see "this class
    has not been built yet" without an external probe.
    """
    cfg = load_config()
    classes = cfg.embedding.classes
    index_dir = _resolve_action_index_dir(project_root)

    # Resolve the on-disk index's current class binding, if any.
    on_disk_class: str | None = None
    on_disk_count = 0
    on_disk_last_built = "(never)"
    db_path = index_dir / "index.db"
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            try:
                meta_rows = con.execute(
                    "SELECT key, value FROM meta"
                ).fetchall()
                meta = {k: v for k, v in meta_rows}
                on_disk_class = meta.get("model_class")
            finally:
                con.close()
            if on_disk_class:
                on_disk_count, on_disk_last_built = _read_index_state(index_dir)
        except (sqlite3.DatabaseError, OSError):
            pass

    st_cache_dir = _resolve_st_cache_dir()
    st_cache_size = _dir_size_mb(st_cache_dir)
    index_size = _dir_size_mb(index_dir)

    rows: list[ClassRow] = []
    for class_name, spec in sorted(classes.items()):
        backend = _backend_for_model(spec.model)
        # The cache_path column shows where each backend's persistent
        # state lives: the action index SQLite for all classes, plus
        # the HF model cache for sentence-transformers entries.
        if backend == "sentence-transformers":
            cache_path = str(st_cache_dir)
            size_mb = st_cache_size + index_size
        else:
            cache_path = str(index_dir)
            size_mb = index_size

        # Only the class that the on-disk meta currently records gets
        # the indexed_actions / last_built numbers; others get the
        # zero/never defaults so the operator sees "not built yet"
        # without ambiguity about who's the current owner.
        if class_name == on_disk_class:
            indexed_actions = on_disk_count
            last_built = on_disk_last_built
        else:
            indexed_actions = 0
            last_built = "(never)"

        rows.append(ClassRow(
            name=class_name,
            backend=backend,
            model=spec.model,
            cache_path=cache_path,
            size_mb=size_mb,
            indexed_actions=indexed_actions,
            last_built=last_built,
        ))
    return rows


# ── output rendering ──────────────────────────────────────────────────────────


def _print_table(rows: list[ClassRow]) -> None:
    """Render ``rows`` as an aligned plain-text table."""
    if not rows:
        print("No embedding classes configured.")
        return
    name_w = max(max(len(r.name) for r in rows), 4)
    backend_w = max(max(len(r.backend) for r in rows), 7)
    model_w = max(max(len(r.model) for r in rows), 5)
    cache_w = max(max(len(r.cache_path) for r in rows), 5)
    header = (
        f"{'NAME':<{name_w}}  {'BACKEND':<{backend_w}}  "
        f"{'MODEL':<{model_w}}  {'CACHE_PATH':<{cache_w}}  "
        f"{'SIZE_MB':>8}  {'ACTIONS':>7}  LAST_BUILT"
    )
    print(header)
    print("─" * len(header))
    for r in rows:
        print(
            f"{r.name:<{name_w}}  {r.backend:<{backend_w}}  "
            f"{r.model:<{model_w}}  {r.cache_path:<{cache_w}}  "
            f"{r.size_mb:>8.2f}  {r.indexed_actions:>7}  {r.last_built}"
        )


def _emit_json(rows: list[ClassRow]) -> None:
    """Emit ``rows`` as a JSON list of dicts (= same field names)."""
    print(json.dumps(
        [
            {
                "name": r.name,
                "backend": r.backend,
                "model": r.model,
                "cache_path": r.cache_path,
                "size_mb": r.size_mb,
                "indexed_actions": r.indexed_actions,
                "last_built": r.last_built,
            }
            for r in rows
        ],
        indent=2,
        ensure_ascii=False,
    ))


# ── subcommand runners ───────────────────────────────────────────────────────


def run_status(args: argparse.Namespace) -> None:
    """``reyn embeddings status`` — show configured classes + index state."""
    project_root = _get_project_root()
    rows = _collect_status_rows(project_root)
    if getattr(args, "json", False):
        _emit_json(rows)
    else:
        _print_table(rows)


def run_rebuild(args: argparse.Namespace) -> None:
    """``reyn embeddings rebuild [<name>]`` — drop the SQLite cache.

    The next chat session ``build()`` call will re-embed the catalog.
    Does NOT itself trigger the embedding API; it only removes the
    cached state. ``<name>`` is accepted for forward compatibility
    with a future per-class cache (the SQLite layout today is shared
    across classes); when supplied, we verify the class exists in
    config and emit a note so the operator knows the broader cache
    was wiped.
    """
    project_root = _get_project_root()
    cfg = load_config()

    target = getattr(args, "name", None)
    if target is not None and target not in cfg.embedding.classes:
        print(
            f"error: embedding class {target!r} not in reyn.yaml's "
            f"embedding.classes. Configured: "
            f"{sorted(cfg.embedding.classes)}"
        )
        raise SystemExit(2)

    index_dir = _resolve_action_index_dir(project_root)
    db_path = index_dir / "index.db"
    if not db_path.exists():
        print(f"no action index found at {db_path}; nothing to rebuild.")
        return

    # Drop the DB and the WAL/SHM sidecars; leave the directory itself
    # so the build lock helper can re-create files on next run.
    removed: list[str] = []
    for sidecar in ("index.db", "index.db-wal", "index.db-shm",
                    ".build.lock"):
        p = index_dir / sidecar
        if p.exists():
            try:
                p.unlink()
                removed.append(sidecar)
            except OSError as exc:
                print(f"warning: could not remove {p}: {exc}")

    if target is not None:
        print(
            f"removed action index cache (shared across classes today): "
            f"{', '.join(removed) or '(nothing)'}. "
            f"The next chat session will re-embed for class {target!r}."
        )
    else:
        print(
            f"removed action index cache: "
            f"{', '.join(removed) or '(nothing)'}. "
            f"The next chat session will re-embed."
        )


def run_clear(args: argparse.Namespace) -> None:
    """``reyn embeddings clear`` — wipe action index + sentence-transformers cache.

    Aggressive: removes the entire ``.reyn/action_index/`` directory
    AND the sentence-transformers HF model cache resolved per the
    REYN_CACHE_DIR / XDG_CACHE_HOME precedence. Useful for "the cache
    is corrupted" or "I want to switch backends and reclaim disk".
    """
    project_root = _get_project_root()
    index_dir = _resolve_action_index_dir(project_root)
    st_cache_dir = _resolve_st_cache_dir()

    removed_bytes_mb = 0.0
    for target in (index_dir, st_cache_dir):
        if target.exists():
            removed_bytes_mb += _dir_size_mb(target)
            try:
                shutil.rmtree(target)
                print(f"removed {target}")
            except OSError as exc:
                print(f"warning: could not remove {target}: {exc}")
        else:
            print(f"skip {target} (= absent)")

    if removed_bytes_mb > 0:
        print(f"freed ~{removed_bytes_mb:.2f} MB")


# ── argparse registration ─────────────────────────────────────────────────────


def register(sub: Any) -> None:
    """Register the ``reyn embeddings`` subcommand."""
    p = sub.add_parser(
        "embeddings",
        help="Inspect and manage the action embedding index",
        description=(
            "Inspect and manage Reyn's action embedding index "
            "(= the SQLite cache backing search_actions). See "
            "`reyn embeddings status` for current state."
        ),
    )
    inner = p.add_subparsers(
        dest="embeddings_command",
        metavar="<subcommand>",
    )
    inner.required = True

    # status
    status = inner.add_parser(
        "status",
        help="Show configured embedding classes + index state",
        description=(
            "Show one row per configured embedding class with "
            "backend / model / cache_path / size_mb / indexed_actions "
            "/ last_built. Reads on-disk state only — no network."
        ),
    )
    status.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of an aligned table.",
    )
    status.set_defaults(func=run_status)

    # rebuild
    rebuild = inner.add_parser(
        "rebuild",
        help="Drop the action index cache so the next session re-embeds",
        description=(
            "Remove the cached action index SQLite and build-lock "
            "marker. Does NOT itself trigger embedding; the next "
            "ChatSession that uses search_actions will re-embed."
        ),
    )
    rebuild.add_argument(
        "name", nargs="?", default=None,
        help=(
            "Optional class name to target. Today the SQLite cache "
            "is shared across classes; passing a name verifies it "
            "exists in reyn.yaml and notes the broader cache wipe."
        ),
    )
    rebuild.set_defaults(func=run_rebuild)

    # clear
    clear = inner.add_parser(
        "clear",
        help="Wipe the action index and the local model cache directory",
        description=(
            "Remove .reyn/action_index/ AND the sentence-transformers "
            "model cache directory. Aggressive: useful for cache "
            "corruption or backend swap reclamation."
        ),
    )
    clear.set_defaults(func=run_clear)

    p.set_defaults(func=lambda _a: p.print_help())
