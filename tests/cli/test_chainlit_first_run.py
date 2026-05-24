"""Tier 1: ``reyn.chainlit_app.first_run.ensure_chainlit_md`` contract.

The CLI calls this before launching chainlit so the operator's first
visit sees a reyn-branded welcome rather than chainlit's generic
boilerplate. Two invariants pinned:

1. **Initial drop**: in an empty cwd, the shipped asset is copied in
   and the destination path is returned.
2. **Idempotent**: a second call on the same cwd is a no-op and
   returns ``None`` — operator edits between launches are preserved.
"""
from __future__ import annotations

from pathlib import Path

from reyn.chainlit_app.first_run import assets_dir, ensure_chainlit_md


def test_assets_dir_contains_chainlit_md():
    """Tier 1: the shipped asset exists at the expected path."""
    src = assets_dir() / "chainlit.md"
    assert src.is_file(), f"shipped chainlit.md missing at {src}"


def test_ensure_chainlit_md_creates_when_absent(tmp_path: Path):
    """Tier 1: empty cwd → copy happens, destination path returned."""
    result = ensure_chainlit_md(tmp_path)
    assert result == tmp_path / "chainlit.md"
    assert result.is_file()
    # Content equality — same bytes as the shipped asset.
    assert (
        result.read_bytes()
        == (assets_dir() / "chainlit.md").read_bytes()
    )


def test_ensure_chainlit_md_skips_when_present(tmp_path: Path):
    """Tier 1: existing file → no overwrite, returns None (= operator
    customization preserved across launches)."""
    custom = tmp_path / "chainlit.md"
    custom.write_text("# operator's custom welcome\n")
    result = ensure_chainlit_md(tmp_path)
    assert result is None
    assert custom.read_text() == "# operator's custom welcome\n"


def test_ensure_chainlit_md_creates_target_dir(tmp_path: Path):
    """Tier 1: missing target dir → mkdir(parents=True) makes it."""
    nested = tmp_path / "deeply" / "nested" / "cwd"
    assert not nested.exists()
    result = ensure_chainlit_md(nested)
    assert result is not None
    assert nested.is_dir()
    assert (nested / "chainlit.md").is_file()
