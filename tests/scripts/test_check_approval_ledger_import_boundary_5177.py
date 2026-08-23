"""Tier 2: #5177 — the approval_ledger.py stdlib-only import boundary gate.

Real filesystem fixtures throughout (a real `tmp_path` `.py` file) — the
function under test reads real file content and parses real ASTs, so
faking the filesystem would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_approval_ledger_import_boundary import reyn_internal_imports


def test_a_direct_module_level_reyn_import_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE case #5177 exists to catch — a future edit adding a
    plain `import reyn.foo` to this file."""
    target = tmp_path / "approval_ledger.py"
    target.write_text("import reyn.runtime.session\n", encoding="utf-8")
    offenders = reyn_internal_imports(target)
    assert offenders == ["reyn.runtime.session"]


def test_a_from_reyn_submodule_import_is_flagged(tmp_path: Path) -> None:
    """Tier 2: `from reyn.config import loader` shape — a submodule import,
    not the bare package name."""
    target = tmp_path / "approval_ledger.py"
    target.write_text("from reyn.config import loader\n", encoding="utf-8")
    offenders = reyn_internal_imports(target)
    assert offenders == ["reyn.config"]


def test_a_deferred_function_local_reyn_import_is_also_flagged(tmp_path: Path) -> None:
    """Tier 2: a deferred (function-local) import is in scope too — a NEW
    reyn-internal import must be caught regardless of whether it is
    module-level or deferred, since a regression could reach for either
    shape (this module's own existing deferred `import yaml` shows
    deferred imports are already a real pattern here, not hypothetical)."""
    target = tmp_path / "approval_ledger.py"
    target.write_text(
        "def f():\n    import reyn.plugins.tokens\n    return reyn.plugins.tokens\n",
        encoding="utf-8",
    )
    offenders = reyn_internal_imports(target)
    assert offenders == ["reyn.plugins.tokens"]


def test_a_bare_reyn_import_with_no_submodule_is_flagged(tmp_path: Path) -> None:
    """Tier 2: `import reyn` (the bare package name, no dotted submodule)
    must also be flagged — the exact-match half of the exact-match-or-
    dotted-prefix rule."""
    target = tmp_path / "approval_ledger.py"
    target.write_text("import reyn\n", encoding="utf-8")
    offenders = reyn_internal_imports(target)
    assert offenders == ["reyn"]


def test_stdlib_and_third_party_imports_are_not_flagged(tmp_path: Path) -> None:
    """Tier 2: the module's own real import set (json/os/tempfile/time/
    pathlib/typing, plus the deferred third-party `import yaml`) must not
    false-positive — only a module named exactly `reyn` or `reyn.<sub>`
    counts."""
    target = tmp_path / "approval_ledger.py"
    target.write_text(
        "import json\nimport os\nimport tempfile\nimport time\n"
        "from pathlib import Path\nfrom typing import Any\n\n"
        "def f():\n    import yaml\n    return yaml\n",
        encoding="utf-8",
    )
    offenders = reyn_internal_imports(target)
    assert offenders == []


def test_a_package_named_reyn_prefixed_is_not_confused_with_reyn(tmp_path: Path) -> None:
    """Tier 2: non-vacuity for the exact-match / dotted-prefix rule — a
    hypothetical unrelated package whose name merely STARTS WITH the same
    letters (e.g. `reynolds`, not a real package, but the rule must not
    match on a bare substring) is not flagged."""
    target = tmp_path / "approval_ledger.py"
    target.write_text("import reynolds\n", encoding="utf-8")
    offenders = reyn_internal_imports(target)
    assert offenders == []


def test_an_absent_file_reads_as_compliant_not_an_error(tmp_path: Path) -> None:
    """Tier 2: a path that does not exist must not raise — the sibling
    boundary gates all treat "nothing there" as "nothing to flag", never
    a crash."""
    target = tmp_path / "does_not_exist.py"
    assert reyn_internal_imports(target) == []


def test_the_real_approval_ledger_module_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current file (not assumed), matching the sibling gates' own
    "run it before shipping it" discipline. #5173 measured this by hand
    (approval_ledger.py has zero reyn-internal imports); this asserts it
    stayed that way."""
    from scripts.check_approval_ledger_import_boundary import (
        _APPROVAL_LEDGER_PATH,
        _ROOT,
    )

    assert _APPROVAL_LEDGER_PATH == (
        _ROOT / "src" / "reyn" / "security" / "permissions" / "approval_ledger.py"
    )
    offenders = reyn_internal_imports(_APPROVAL_LEDGER_PATH)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
