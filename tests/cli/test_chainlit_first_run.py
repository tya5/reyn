"""Tier 1: ``reyn.interfaces.chainlit_app.first_run`` asset-copy contracts.

The CLI calls these helpers before launching chainlit so the operator
sees reyn defaults (welcome page + UI tweaks) instead of chainlit's
generic boilerplate. Two invariants per helper:

1. **Initial drop**: empty cwd → asset copied in, destination returned.
2. **Idempotent**: second call → no-op, returns ``None`` (= operator
   edits between launches preserved).
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.chainlit_app.first_run import (
    assets_dir,
    ensure_all_assets,
    ensure_chainlit_config,
    ensure_chainlit_md,
    ensure_public_css,
)


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


# ── ensure_chainlit_config ────────────────────────────────────────────────


def test_assets_dir_contains_chainlit_config():
    """Tier 1: shipped ``.chainlit/config.toml`` template exists."""
    src = assets_dir() / ".chainlit" / "config.toml"
    assert src.is_file(), f"shipped config.toml missing at {src}"


def test_config_template_has_required_meta_for_chainlit_loader():
    """Tier 1: chainlit's ``load_settings`` raises if ``[meta].generated_by``
    is absent / <= ``0.3.0``. Pin the shipped template's marker so a
    future edit doesn't accidentally break the loader."""
    src = (assets_dir() / ".chainlit" / "config.toml").read_text()
    assert "[meta]" in src
    assert "generated_by" in src


def test_config_template_disables_confirm_new_chat():
    """Tier 1: pin the actual override that motivates the template —
    a stray formatting change shouldn't silently re-enable the popup."""
    src = (assets_dir() / ".chainlit" / "config.toml").read_text()
    assert "confirm_new_chat = false" in src


def test_ensure_chainlit_config_creates_when_absent(tmp_path: Path):
    """Tier 1: empty cwd → copy + parent ``.chainlit/`` mkdir + return path."""
    result = ensure_chainlit_config(tmp_path)
    assert result == tmp_path / ".chainlit" / "config.toml"
    assert result.is_file()
    assert (tmp_path / ".chainlit").is_dir()


def test_ensure_chainlit_config_skips_when_present(tmp_path: Path):
    """Tier 1: existing file → no overwrite, returns None (= operator
    customization preserved across launches)."""
    cdir = tmp_path / ".chainlit"
    cdir.mkdir()
    custom = cdir / "config.toml"
    custom.write_text("# operator's config\n")
    result = ensure_chainlit_config(tmp_path)
    assert result is None
    assert custom.read_text() == "# operator's config\n"


# ── ensure_all_assets (= CLI entry path) ──────────────────────────────────


def test_ensure_all_assets_copies_both_on_first_run(tmp_path: Path):
    """Tier 1: clean cwd → all 3 shipped assets (chainlit.md /
    .chainlit/config.toml / public/reyn.css) land."""
    written = ensure_all_assets(tmp_path)
    written_names = sorted(p.name for p in written)
    assert written_names == ["chainlit.md", "config.toml", "reyn.css"]
    assert (tmp_path / "chainlit.md").is_file()
    assert (tmp_path / ".chainlit" / "config.toml").is_file()
    assert (tmp_path / "public" / "reyn.css").is_file()


def test_ensure_all_assets_noop_when_both_present(tmp_path: Path):
    """Tier 1: all 3 files pre-existing → returns empty list, no overwrites."""
    (tmp_path / "chainlit.md").write_text("custom welcome")
    (tmp_path / ".chainlit").mkdir()
    (tmp_path / ".chainlit" / "config.toml").write_text("custom config")
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "reyn.css").write_text("/* custom css */")
    written = ensure_all_assets(tmp_path)
    assert written == []
    assert (tmp_path / "chainlit.md").read_text() == "custom welcome"
    assert (tmp_path / ".chainlit" / "config.toml").read_text() == "custom config"
    assert (tmp_path / "public" / "reyn.css").read_text() == "/* custom css */"


def test_ensure_all_assets_partial_one_existing_one_missing(tmp_path: Path):
    """Tier 1: one asset pre-existing, the others not → copy only the
    missing ones (= per-asset idempotence, not all-or-nothing)."""
    (tmp_path / "chainlit.md").write_text("custom welcome")
    written = ensure_all_assets(tmp_path)
    # config.toml + public/reyn.css missing, chainlit.md preserved → copy
    # only the 2 missing ones (= name-set comparison rather than count
    # alone so the contract is the actual set, not its cardinality).
    written_names = sorted(p.name for p in written)
    assert written_names == ["config.toml", "reyn.css"]
    assert (tmp_path / "chainlit.md").read_text() == "custom welcome"


# ── ensure_public_css ─────────────────────────────────────────────────────


def test_assets_dir_contains_public_reyn_css():
    """Tier 1: shipped ``public/reyn.css`` template exists."""
    src = assets_dir() / "public" / "reyn.css"
    assert src.is_file(), f"shipped reyn.css missing at {src}"


def test_public_css_hides_new_chat_button_selectors():
    """Tier 1: pin the actual visible-hide rule — a stray formatting
    change shouldn't silently lose the selector that does the work."""
    src = (assets_dir() / "public" / "reyn.css").read_text()
    assert "display: none" in src
    # Multiple aria-label / data-tooltip selectors so chainlit version
    # drift in localised strings or test-id renames doesn't silently
    # re-show the button.
    assert "New Chat" in src
    assert "newChat" in src


def test_config_template_points_at_public_reyn_css():
    """Tier 1: shipped ``.chainlit/config.toml`` references the same
    relative path the ``ensure_public_css`` helper writes to. A
    rename on either side without updating the other would leave the
    rule loaded but the file missing."""
    src = (assets_dir() / ".chainlit" / "config.toml").read_text()
    assert "/public/reyn.css" in src
    assert "custom_css" in src


def test_ensure_public_css_creates_when_absent(tmp_path: Path):
    """Tier 1: empty cwd → copy + parent ``public/`` mkdir + return path."""
    result = ensure_public_css(tmp_path)
    assert result == tmp_path / "public" / "reyn.css"
    assert result.is_file()
    assert (tmp_path / "public").is_dir()


def test_ensure_public_css_skips_when_present(tmp_path: Path):
    """Tier 1: existing file → no overwrite, returns None."""
    pdir = tmp_path / "public"
    pdir.mkdir()
    custom = pdir / "reyn.css"
    custom.write_text("/* operator's css */\n")
    result = ensure_public_css(tmp_path)
    assert result is None
    assert custom.read_text() == "/* operator's css */\n"


def test_ensure_all_assets_includes_public_css(tmp_path: Path):
    """Tier 1: clean cwd → all 3 shipped assets land."""
    written = ensure_all_assets(tmp_path)
    written_names = sorted(p.name for p in written)
    assert written_names == ["chainlit.md", "config.toml", "reyn.css"]
    assert (tmp_path / "chainlit.md").is_file()
    assert (tmp_path / ".chainlit" / "config.toml").is_file()
    assert (tmp_path / "public" / "reyn.css").is_file()
