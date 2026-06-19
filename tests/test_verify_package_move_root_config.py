"""Tier 2: verify_package_move scans repo-root config (entry points / paths).

Backstop for the #1807 miss: the tool was run with ``--roots src tests docs``,
so ``pyproject.toml``'s entry points (still naming the deleted ``reyn.plugins``)
were never scanned and the broken webhook discovery shipped. ``ROOT_CONFIG_FILES``
is now always included in the dotted-literal / path checks.
"""
from __future__ import annotations

import pytest

from scripts.verify_package_move import main


def _write_pyproject(tmp_path, dotted: str) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project.entry-points."reyn.webhooks"]\n'
        f'sample = "{dotted}:register_router"\n'
    )


def test_root_config_pyproject_is_scanned(tmp_path, monkeypatch):
    """Tier 2: a stale dotted ref in pyproject.toml is caught even when --roots
    excludes the repo root (the #1807 entry-point regression class)."""
    _write_pyproject(tmp_path, "old.pkg.sample")
    monkeypatch.chdir(tmp_path)
    # package-move mode (no symbol); src is empty, so the ONLY hit is pyproject.
    assert main(["old.pkg", "--roots", "src"]) == 1


def test_clean_root_config_passes(tmp_path, monkeypatch):
    """Tier 2: no residual reference in root config → clean exit."""
    _write_pyproject(tmp_path, "new.pkg.sample")
    monkeypatch.chdir(tmp_path)
    assert main(["old.pkg", "--roots", "src"]) == 0
