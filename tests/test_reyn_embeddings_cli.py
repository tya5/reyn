"""Tier 2: FP-0043 Component C.2 — ``reyn embeddings`` CLI contract.

Pins the operator-facing CLI surface:

  1. ``reyn embeddings status`` renders one row per configured embedding
     class with the agreed columns (name / backend / model / cache_path /
     size_mb / indexed_actions / last_built).
  2. ``--json`` emits a JSON list matching the table column names so
     downstream scripts can aggregate without parsing the table.
  3. Backend column is derived from the ``model`` prefix (= "litellm"
     for non-prefixed / "sentence-transformers" for the ``sentence-
     transformers/`` prefix).
  4. ``reyn embeddings rebuild`` drops the on-disk action index SQLite
     + the build-lock marker; a missing index reports "nothing to
     rebuild" without erroring.
  5. ``reyn embeddings rebuild <name>`` rejects unknown class names.
  6. ``reyn embeddings clear`` removes the action index directory AND
     the sentence-transformers cache directory; absent paths are
     reported as skipped.

No mocks. Tests work against real on-disk state in ``tmp_path`` with
``monkeypatch.chdir(tmp_path)`` so the CLI's cwd-based project-root
resolution is exercised end-to-end.
"""
from __future__ import annotations

import json
import struct
from argparse import Namespace
from pathlib import Path

import pytest

from reyn.cli.commands.embeddings import (
    _backend_for_model,
    _collect_status_rows,
    run_clear,
    run_rebuild,
    run_status,
)


def _write_index_db(
    index_dir: Path, class_name: str, vectors: dict[str, list[float]],
) -> None:
    """Stand up a real SQLite ``index.db`` matching ActionEmbeddingIndex shape."""
    import sqlite3
    index_dir.mkdir(parents=True, exist_ok=True)
    db_path = index_dir / "index.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS vectors "
            "(qualified_name TEXT PRIMARY KEY, item_json TEXT NOT NULL, "
            "vector_blob BLOB NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('model_class', ?)",
            (class_name,),
        )
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('catalog_hash', 'h')",
        )
        for qn, vec in vectors.items():
            con.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                [(qn, '{}', struct.pack(f"{len(vec)}d", *vec))],
            )
        con.commit()
    finally:
        con.close()


# ── 1. backend resolution ─────────────────────────────────────────────────────


def test_backend_for_litellm_prefix() -> None:
    """Tier 2: non-prefixed model string → litellm backend."""
    assert _backend_for_model("openai/text-embedding-3-small") == "litellm"


def test_backend_for_sentence_transformers_prefix() -> None:
    """Tier 2: ``sentence-transformers/`` prefix → sentence-transformers backend."""
    assert (
        _backend_for_model("sentence-transformers/all-MiniLM-L6-v2")
        == "sentence-transformers"
    )


# ── 2. status row collection ────────────────────────────────────────────────


def test_status_rows_include_every_configured_class(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: every class in ``embedding.classes`` produces a row."""
    monkeypatch.chdir(tmp_path)
    rows = _collect_status_rows(tmp_path)
    names = {r.name for r in rows}
    # Defaults from FP-0043 Phase 2 (= local-mini + local-e5 added).
    for expected in ("light", "standard", "strong", "local-mini", "local-e5"):
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
    index_dir = tmp_path / ".reyn" / "action_index"
    _write_index_db(
        index_dir,
        class_name="local-mini",
        vectors={"file__read": [0.1, 0.2], "web__search": [0.3, 0.4]},
    )
    rows = _collect_status_rows(tmp_path)
    by_name = {r.name: r for r in rows}
    assert by_name["local-mini"].indexed_actions == 2
    assert by_name["local-mini"].last_built != "(never)"
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
    index_dir = tmp_path / ".reyn" / "action_index"
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
    'lcal-mini' (= typo of local-mini) and nothing changed").
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
    index_dir = tmp_path / ".reyn" / "action_index"
    _write_index_db(index_dir, "local-mini", {"file__read": [0.1, 0.2]})
    run_rebuild(Namespace(name="local-mini"))
    out = capsys.readouterr().out
    assert "local-mini" in out
    assert "shared across classes" in out


# ── 5. clear ────────────────────────────────────────────────────────────────


def test_clear_removes_index_dir_and_st_cache_dir(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: clear removes BOTH the action index dir AND the ST model cache.

    Both directories are explicitly cleared; absent paths are reported
    as skipped (= no crash on a clean install).
    """
    # Override the ST cache so it's under tmp_path and we can verify removal
    # without touching the developer's real ~/.cache.
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path / "reyn-cache"))
    monkeypatch.chdir(tmp_path)
    index_dir = tmp_path / ".reyn" / "action_index"
    _write_index_db(index_dir, "standard", {"file__read": [0.1, 0.2]})
    st_cache = tmp_path / "reyn-cache" / "sentence-transformers"
    st_cache.mkdir(parents=True)
    (st_cache / "model.bin").write_bytes(b"\x00" * 1024)

    run_clear(Namespace())
    out = capsys.readouterr().out

    assert not index_dir.exists(), "index dir should have been removed"
    assert not st_cache.exists(), "ST cache dir should have been removed"
    assert "removed" in out
    assert "freed" in out


def test_clear_on_absent_paths_reports_skip(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: clear on a clean project skips gracefully without error."""
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path / "reyn-cache-empty"))
    monkeypatch.chdir(tmp_path)
    run_clear(Namespace())
    out = capsys.readouterr().out
    # Both targets absent → two skip lines, no failure.
    assert out.count("skip") >= 2
