"""Tier 2 end-to-end coverage for `reyn.memory.memory.rewrite_index`.

The function is the public CLI seam called by `reyn memory edit / delete /
import` after every mutation, but it was silently broken on a
`from .op_runtime.file import regenerate_index_impl` (relative path that
does not resolve — `op_runtime` lives at `reyn.op_runtime`, not
`reyn.memory.op_runtime`). The bug surfaced while drafting
`tests/test_memory_invariants.py` whose tests bypass `rewrite_index` and
call `regenerate_index_impl` directly. This file pins the entire seam.
"""
from __future__ import annotations

from pathlib import Path

from reyn.memory.memory import (
    INDEX_FILENAME,
    INDEX_HEADER,
    render_body,
    rewrite_index,
)


def _write_entry(scope: Path, slug: str, *, name: str, description: str) -> None:
    body = render_body(name=name, description=description, type_="project", body="x")
    (scope / f"{slug}.md").write_text(body, encoding="utf-8")


def test_rewrite_index_resolves_op_runtime_import(tmp_path: Path) -> None:
    """Tier 2: rewrite_index runs to completion and produces MEMORY.md.

    Regression for the broken `from .op_runtime.file` relative import that
    raised ModuleNotFoundError before the fix.
    """
    _write_entry(tmp_path, "alpha", name="Alpha Note", description="first entry")
    _write_entry(tmp_path, "beta", name="Beta Note", description="second entry")

    rewrite_index(tmp_path)

    index = tmp_path / INDEX_FILENAME
    assert index.exists(), "rewrite_index did not produce MEMORY.md"

    text = index.read_text(encoding="utf-8")
    assert text.startswith(INDEX_HEADER), "header constant not preserved"
    assert "Alpha Note" in text
    assert "Beta Note" in text
    assert "(alpha.md)" in text
    assert "(beta.md)" in text


def test_rewrite_index_excludes_self(tmp_path: Path) -> None:
    """Tier 2: MEMORY.md must not list itself as an entry."""
    _write_entry(tmp_path, "only", name="Only Entry", description="solo")

    # First call seeds MEMORY.md; second call must not double-count it.
    rewrite_index(tmp_path)
    rewrite_index(tmp_path)

    text = (tmp_path / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "(MEMORY.md)" not in text
    # entry count = 1 line (single bullet)
    bullets = [ln for ln in text.splitlines() if ln.startswith("- [")]
    assert len(bullets) == 1, f"expected 1 entry bullet, got {len(bullets)}: {bullets}"


def test_rewrite_index_reflects_deletion(tmp_path: Path) -> None:
    """Tier 2: deleting a body file + rewrite removes its index entry."""
    _write_entry(tmp_path, "keep", name="Keeper", description="stays")
    _write_entry(tmp_path, "drop", name="Dropper", description="goes")

    rewrite_index(tmp_path)
    assert "Dropper" in (tmp_path / INDEX_FILENAME).read_text(encoding="utf-8")

    (tmp_path / "drop.md").unlink()
    rewrite_index(tmp_path)

    text = (tmp_path / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "Keeper" in text
    assert "Dropper" not in text
