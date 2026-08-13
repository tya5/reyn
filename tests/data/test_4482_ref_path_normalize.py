"""Tier 2: #4482 PR-1 — ``normalize_ref_path``, the single canonicalization
function both ref minting and the ref → path lookup table will call
(architect's review: split across two call sites and the same file gets
two refs). This test file exercises the function directly, in isolation
from the ref table itself (not written yet — this is the independent,
no-dependency slice of PR-1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from reyn.data.workspace.ref_path_normalize import normalize_ref_path


def test_relative_and_absolute_spellings_normalize_identically(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — a relative path and its
    absolute spelling of the SAME file must not diverge."""
    target = tmp_path / "sub" / "report.pptx"
    target.parent.mkdir()
    target.write_bytes(b"x")

    via_relative = normalize_ref_path("sub/report.pptx", tmp_path)
    via_absolute = normalize_ref_path(target, tmp_path)

    assert via_relative == via_absolute


def test_symlink_normalizes_to_the_same_form_as_its_target(tmp_path: Path):
    """Tier 2: the acceptance criterion, verbatim — a symlink and its real
    target must not diverge."""
    real = tmp_path / "real.txt"
    real.write_bytes(b"x")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    assert normalize_ref_path(link, tmp_path) == normalize_ref_path(real, tmp_path)


def test_dot_and_dotdot_components_collapse(tmp_path: Path):
    """Tier 2: '.'/'..' spellings of the same file normalize identically —
    Path.resolve()'s own collapsing, exercised through this function."""
    target = tmp_path / "a" / "b" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    messy = tmp_path / "a" / "b" / ".." / "b" / "." / "file.txt"
    clean = target

    assert normalize_ref_path(messy, tmp_path) == normalize_ref_path(clean, tmp_path)


def test_idempotent_normalizing_twice_is_a_noop(tmp_path: Path):
    """Tier 2: (accept-side) normalizing an already-normalized path returns
    the same value — normalization is a fixed point, not a one-shot
    transform that could drift on re-application."""
    target = tmp_path / "file.txt"
    target.write_bytes(b"x")

    once = normalize_ref_path(target, tmp_path)
    twice = normalize_ref_path(once, tmp_path)
    assert once == twice


def test_case_differing_paths_diverge_on_a_case_sensitive_filesystem():
    """Tier 2: (accept-side, POSIX) two different-case spellings are
    DIFFERENT files on a case-sensitive filesystem (the CI/Linux default)
    — os.path.normcase is a no-op there, so this function must not
    conflate them. Confirms the documented scope: this function folds
    case only where os.path.normcase itself would."""
    if sys.platform == "win32":
        pytest.skip("this accept-side case only holds where normcase is a no-op (POSIX)")

    project_root = Path("/tmp")
    lower = normalize_ref_path("Report.PPTX", project_root)
    upper = normalize_ref_path("report.pptx", project_root)
    assert str(lower) != str(upper)


def test_does_not_require_the_path_to_exist(tmp_path: Path):
    """Tier 2: normalization is pure path algebra — a not-yet-written
    (or already-deleted) target still normalizes, no exception. Existence
    is the LOOKUP side's separate concern, not this function's."""
    missing = tmp_path / "does" / "not" / "exist.txt"
    result = normalize_ref_path(missing, tmp_path)
    assert result.is_absolute()


def test_windows_style_case_folding_documented_via_normcase(tmp_path: Path, monkeypatch):
    """Tier 2: this function's case-folding behavior is exactly
    os.path.normcase's — verified by making normcase itself report a
    folded form (simulating what Windows' real normcase does) and
    confirming normalize_ref_path's output reflects it, rather than
    re-implementing its own case logic that could drift from the stdlib
    one it claims to delegate to."""
    target = tmp_path / "File.TXT"
    target.write_bytes(b"x")

    monkeypatch.setattr(os.path, "normcase", str.lower)
    result = normalize_ref_path(target, tmp_path)
    assert str(result) == str(target.resolve()).lower()
