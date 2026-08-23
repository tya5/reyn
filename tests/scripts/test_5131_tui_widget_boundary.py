"""Tier 2: #5131 gate A — scripts/check_tui_widget_boundary.py.

Architect/lead-coder review (issuecomment-5384396179, broker 2026-08-23
05:23Z): the ratchet tests (test_5131_tui_reactive_ratchet.py) covered gate
B; gate A had NO red witness — every existing check only confirmed the gate
is green on the ALREADY-COMPLIANT current tree, which cannot distinguish
"the gate is enforcing the boundary" from "the gate never runs at all" (a
green with nothing to bite on wears the same colour either way — CLAUDE.md's
own test-review question 4). This file closes that: a synthetic fixture
widget module that DOES import transport, and asserts BOTH the underlying
detector (find_violations) AND main()'s own CLI exit code catch it.

Real files on a real tmp_path (mirrors test_flat_tests_ratchet_3879.py's own
placement/no-mocks rationale) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import scripts.check_tui_widget_boundary as check_tui_widget_boundary
from scripts.check_tui_widget_boundary import find_violations, main


def _write(package_dir: Path, name: str, content: str) -> None:
    (package_dir / name).write_text(content, encoding="utf-8")


def test_find_violations_catches_a_module_level_import(tmp_path: Path) -> None:
    """Tier 2: the exact shape the gate exists to catch — a widget module
    importing the forbidden transport prefix as a plain module-level
    ``import``."""
    _write(tmp_path, "some_widget.py", "import reyn.interfaces.transport.frames\n")

    violations = find_violations(tmp_path)

    assert violations == [
        (tmp_path / "some_widget.py", "reyn.interfaces.transport.frames"),
    ]


def test_find_violations_catches_a_from_import(tmp_path: Path) -> None:
    """Tier 2: the OTHER import shape — ``from reyn.runtime.registry import
    X`` — both AST node types the detector walks (ast.Import / ast.ImportFrom)
    need their own witness, not just one representative shape."""
    _write(tmp_path, "other_widget.py", "from reyn.runtime.registry import AgentRegistry\n")

    violations = find_violations(tmp_path)

    assert violations == [
        (tmp_path / "other_widget.py", "reyn.runtime.registry"),
    ]


def test_find_violations_ignores_app_py_by_name(tmp_path: Path) -> None:
    """Tier 2: app.py is the ONE exempt file (it IS the wire<->widget-tree
    seam) — the same forbidden import in a file literally named app.py must
    NOT be flagged."""
    _write(tmp_path, "app.py", "import reyn.interfaces.transport.frames\n")

    assert find_violations(tmp_path) == []


def test_find_violations_ignores_a_docstring_merely_mentioning_transport(
    tmp_path: Path,
) -> None:
    """Tier 2: AST-based, not substring — a docstring/comment that MENTIONS
    "transport" in prose (this package's own module docstrings do) must
    never false-positive. Falsification contrast to the module-level-import
    test above: same word present in the file, opposite verdict."""
    _write(
        tmp_path, "prose_widget.py",
        '"""This widget talks to the transport layer conceptually, but '
        'imports nothing from it."""\n',
    )

    assert find_violations(tmp_path) == []


def test_main_exits_nonzero_when_a_widget_module_violates_the_boundary(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the blocking gap itself (architect/lead-coder review) — a
    green find_violations() result is not evidence main()'s own CLI wiring
    ever reaches it. Monkeypatches the MODULE-LEVEL ``_PACKAGE_DIR`` (main()
    does a fresh name lookup, not a bound default — see find_violations's
    own docstring for why that distinction is load-bearing) and drives
    main() itself, asserting the real CLI exit code."""
    _write(tmp_path, "some_widget.py", "import reyn.interfaces.transport.frames\n")
    monkeypatch.setattr(check_tui_widget_boundary, "_PACKAGE_DIR", tmp_path)

    exit_code = main()

    assert exit_code == 1, "main() did not reject a widget module importing transport"


def test_main_exits_zero_on_a_compliant_fixture_package(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast for the test above — a fixture package
    with NO violation still passes through main()'s full CLI path to exit 0,
    so the red witness above is pinned to the violation, not to some other
    difference between the fixture and the real package."""
    _write(tmp_path, "clean_widget.py", "from textual.widgets import Static\n")
    monkeypatch.setattr(check_tui_widget_boundary, "_PACKAGE_DIR", tmp_path)

    assert main() == 0
