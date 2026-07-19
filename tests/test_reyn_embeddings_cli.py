"""Tier 2: FP-0043 Component C.2 — ``reyn embeddings`` CLI contract.

Pins the operator-facing CLI surface:

  1. ``reyn embeddings status`` renders one row per configured embedding
     class with the agreed columns (name / backend / model / cache_path /
     size_mb / indexed_actions / last_built).
  2. ``--json`` emits a JSON list matching the table column names so
     downstream scripts can aggregate without parsing the table.
  3. ``reyn embeddings rebuild`` drops the on-disk action index SQLite
     + the build-lock marker; a missing index reports "nothing to
     rebuild" without erroring.
  4. ``reyn embeddings rebuild <name>`` rejects unknown class names.
  5. ``reyn embeddings clear`` removes the action index directory;
     an absent directory is reported as skipped.

#3128 (PR-C) removed reyn's in-process sentence-transformers backend:
``backend`` is now always ``"litellm"`` (all ``embedding.classes``
route through litellm — see ``src/reyn/config/embedding.py``) and
``clear`` no longer also wipes a downloaded-model cache dir. The
SQLite action-index cache this command manages is shared substrate
with the litellm-fronted embedding path (and any other
``IndexBackend`` source), not ST-specific, so its status/rebuild/clear
management is retained unchanged.

FP-0057 Phase 0 (#2843): the action index now rides the unified
``IndexBackend`` (``chunks``/``meta`` schema, ``.reyn/cache/index/actions/``)
instead of a private ``vectors``/``meta`` schema under
``.reyn/cache/action_index/`` — the fixture writer below stands up the
unified schema directly (real on-disk SQLite, same shape
``SqliteIndexBackend`` writes) rather than going through the CLI's own
production code, so this stays a fixture, not a round-trip test of the
backend itself (that's ``tests/test_index_backend.py``'s job).

No mocks. Tests work against real on-disk state in ``tmp_path`` with
``monkeypatch.chdir(tmp_path)`` so the CLI's cwd-based project-root
resolution is exercised end-to-end.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from reyn.interfaces.cli.commands.embeddings import (
    _collect_status_rows,
    run_clear,
    run_rebuild,
    run_status,
)


def _unified_index_dir(project_root: Path) -> Path:
    return project_root / ".reyn" / "cache" / "index" / "actions"


def _write_index_db(
    index_dir: Path, class_name: str, vectors: dict[str, list[float]],
) -> None:
    """Stand up a real SQLite ``index.db`` matching the unified IndexBackend
    schema (``SqliteIndexBackend``'s ``chunks``/``meta`` tables)."""
    import sqlite3
    index_dir.mkdir(parents=True, exist_ok=True)
    db_path = index_dir / "index.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content_hash TEXT UNIQUE NOT NULL, "
            "text TEXT NOT NULL, vector BLOB NOT NULL, "
            "metadata_json TEXT NOT NULL, source_path TEXT NOT NULL, "
            "source_type TEXT NOT NULL, embedding_model TEXT NOT NULL, "
            "chunk_index INTEGER NOT NULL, size_tokens INTEGER NOT NULL, "
            "parent_context TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('embedding_model', ?)",
            (class_name,),
        )
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_indexed', '2026-01-01T00:00:00+00:00')",
        )
        for qn, vec in vectors.items():
            content_hash = f"hash-{qn}"
            vec_blob = np.asarray(vec, dtype=np.float32).tobytes()
            con.execute(
                "INSERT OR REPLACE INTO chunks "
                "(content_hash, text, vector, metadata_json, source_path, "
                " source_type, embedding_model, chunk_index, size_tokens, "
                " parent_context) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (content_hash, qn, vec_blob, "{}", qn, "action",
                 class_name, 0, 0, None),
            )
        con.commit()
    finally:
        con.close()


# ── 1. backend resolution ─────────────────────────────────────────────────────


def test_status_rows_backend_is_always_litellm(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: every row's backend is "litellm" (#3128: litellm-exclusive)."""
    monkeypatch.chdir(tmp_path)
    rows = _collect_status_rows(tmp_path)
    assert rows
    for row in rows:
        assert row.backend == "litellm"


# ── 2. status row collection ────────────────────────────────────────────────


def test_status_rows_include_every_configured_class(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: every class in ``embedding.classes`` produces a row."""
    monkeypatch.chdir(tmp_path)
    rows = _collect_status_rows(tmp_path)
    names = {r.name for r in rows}
    # Built-in defaults (#3128: all litellm-routed, no local ST classes).
    for expected in ("light", "standard", "strong"):
        assert expected in names, (
            f"class {expected!r} missing from status rows; got {names}"
        )


def test_status_rows_attribute_count_to_on_disk_class_only(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: indexed_actions is attributed to the class recorded in meta only.

    Component E pins one model_class per SQLite cache. The CLI reflects
    this by showing the count + last_built ONLY on the row whose name
    matches the recorded meta.model_class. Other rows show 0 / "(never)".
    """
    monkeypatch.chdir(tmp_path)
    index_dir = _unified_index_dir(tmp_path)
    _write_index_db(
        index_dir,
        class_name="light",
        vectors={"file__read": [0.1, 0.2], "web__search": [0.3, 0.4]},
    )
    rows = _collect_status_rows(tmp_path)
    by_name = {r.name: r for r in rows}
    assert by_name["light"].indexed_actions == 2
    assert by_name["light"].last_built != "(never)"
    # Other classes get zeros — not the foreign class's count.
    assert by_name["standard"].indexed_actions == 0
    assert by_name["standard"].last_built == "(never)"


# ── 3. status / --json output ───────────────────────────────────────────────


def test_status_default_emits_aligned_table(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: ``reyn embeddings status`` prints aligned table with header row.

    Mirrors the ``reyn mcp list`` shape (= O4 resolution per FP-0043
    tui-coder + lead-coder consolidated decision).
    """
    monkeypatch.chdir(tmp_path)
    run_status(Namespace(json=False))
    out = capsys.readouterr().out
    # Header column names present, in order.
    header_idx = out.find("NAME")
    assert header_idx >= 0
    for col in ("BACKEND", "MODEL", "CACHE_PATH", "SIZE_MB",
                "ACTIONS", "LAST_BUILT"):
        assert out.find(col, header_idx) > header_idx, (
            f"column {col!r} missing or out of order"
        )
    # At least one configured class shows up.
    assert "standard" in out or "light" in out


def test_status_json_emits_machine_readable_array(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: ``--json`` emits a list of dicts with the agreed keys."""
    monkeypatch.chdir(tmp_path)
    run_status(Namespace(json=True))
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list) and data
    required_keys = {
        "name", "backend", "model", "cache_path",
        "size_mb", "indexed_actions", "last_built",
    }
    for row in data:
        assert isinstance(row, dict)
        assert required_keys.issubset(row.keys()), (
            f"row missing keys {required_keys - set(row.keys())}: {row}"
        )


# ── 4. rebuild ──────────────────────────────────────────────────────────────


def test_rebuild_removes_index_files_when_present(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: rebuild drops index.db + WAL sidecars + .build.lock."""
    monkeypatch.chdir(tmp_path)
    index_dir = _unified_index_dir(tmp_path)
    _write_index_db(index_dir, "standard", {"file__read": [0.1, 0.2]})
    (index_dir / ".build.lock").write_text("{}", encoding="utf-8")
    run_rebuild(Namespace(name=None))
    out = capsys.readouterr().out
    assert not (index_dir / "index.db").exists()
    assert not (index_dir / ".build.lock").exists()
    assert "removed action index cache" in out


def test_rebuild_no_index_reports_nothing_to_do(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: rebuild on a clean project reports "nothing to rebuild" cleanly.

    Must not raise — operator running rebuild after a `clear` should
    see a friendly message, not a crash.
    """
    monkeypatch.chdir(tmp_path)
    run_rebuild(Namespace(name=None))
    out = capsys.readouterr().out
    assert "nothing to rebuild" in out


def test_rebuild_unknown_class_name_rejects(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: rebuild with a name not in reyn.yaml classes exits non-zero.

    Catches typos and prevents silent no-op confusion (= "I rebuilt
    'standrad' (= typo of standard) and nothing changed").
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        run_rebuild(Namespace(name="nonexistent_class"))
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "nonexistent_class" in out
    assert "Configured:" in out


def test_rebuild_known_class_name_notes_shared_cache(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: rebuild with a valid class name notes the cache-shared semantics.

    Pinned because the per-class cache split is a possible future
    direction; passing the name today verifies the class exists and
    emits a clear note about the shared cache being wiped.
    """
    monkeypatch.chdir(tmp_path)
    index_dir = _unified_index_dir(tmp_path)
    _write_index_db(index_dir, "standard", {"file__read": [0.1, 0.2]})
    run_rebuild(Namespace(name="standard"))
    out = capsys.readouterr().out
    assert "standard" in out
    assert "shared across classes" in out


# ── 5. clear ────────────────────────────────────────────────────────────────


def test_clear_removes_index_dir(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: clear removes the action index cache directory.

    #3128 removed the in-process sentence-transformers backend, so
    ``clear`` no longer also wipes a downloaded-model cache dir — the
    SQLite action index is the only on-disk state it manages.
    """
    monkeypatch.chdir(tmp_path)
    index_dir = _unified_index_dir(tmp_path)
    _write_index_db(index_dir, "standard", {"file__read": [0.1, 0.2]})

    run_clear(Namespace())
    out = capsys.readouterr().out

    assert not index_dir.exists(), "index dir should have been removed"
    assert "removed" in out
    assert "freed" in out


def test_clear_on_absent_path_reports_skip(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: clear on a clean project skips gracefully without error."""
    monkeypatch.chdir(tmp_path)
    run_clear(Namespace())
    out = capsys.readouterr().out
    assert "skip" in out
