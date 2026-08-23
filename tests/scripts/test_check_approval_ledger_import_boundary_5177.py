"""Tier 2: #5177 — the approval_ledger.py stdlib-only import boundary gate.

Real filesystem fixtures throughout (a real `tmp_path` `.py` file) — the
function under test reads real file content and parses real ASTs, so
faking the filesystem would test nothing real.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from reyn.security.permissions import approval_ledger
from scripts.check_approval_ledger_import_boundary import reyn_internal_imports
from tests._support.paths import REPO_ROOT


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
    a crash. This is about a plain FILE READ, not about the module-level
    ``_APPROVAL_LEDGER_PATH`` constant itself, which is a different case —
    see the "derived from __file__" tests below for why THAT path fails
    loud instead when the real module moves."""
    target = tmp_path / "does_not_exist.py"
    assert reyn_internal_imports(target) == []


def test_a_type_checking_guarded_import_is_not_flagged() -> None:
    """Tier 2: architect's non-blocking question on #5183
    (issuecomment-5384441986) — an import inside ``if TYPE_CHECKING:``
    never executes, so it never actually reaches the python-harness
    SUBPROCESS this gate exists to protect. Flagging it would be a false
    positive against this gate's own stated purpose (what gets pulled in
    at REAL runtime), not a genuine widening of the import surface."""
    import ast
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "approval_ledger.py"
        target.write_text(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import reyn.runtime.session\n",
            encoding="utf-8",
        )
        offenders = reyn_internal_imports(target)
    assert offenders == [], (
        "a TYPE_CHECKING-guarded reyn import must not be flagged — it "
        "never executes at real runtime"
    )
    # Non-vacuity: prove the fixture's AST genuinely contains the import
    # this test means to exclude, so a green result isn't just "nothing
    # was ever parsed."
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import reyn.runtime.session\n"
    )
    real_imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert real_imports, "test setup itself must contain a real Import node"


def test_the_target_path_is_derived_from_the_real_modules_own_file_not_hand_typed() -> None:
    """Tier 2: acceptance (architect blocking finding on #5183,
    issuecomment-5384441986) — ``_APPROVAL_LEDGER_PATH`` must be DERIVED
    from ``approval_ledger.__file__`` (a real import), never a hand-typed
    literal. A hand-typed path silently reads a rename/move as "absent =
    compliant" (this gate's own file-read rule, above) instead of failing
    loud — the exact class #5175 (this issue's own sibling fix) closed
    for the write-gate carve-out, the same night."""
    from scripts.check_approval_ledger_import_boundary import _APPROVAL_LEDGER_PATH

    assert _APPROVAL_LEDGER_PATH == Path(approval_ledger.__file__).resolve(), (
        "the gate's target path must equal the REAL module's own __file__, "
        "not a separately-typed literal that could silently drift from it"
    )


def test_a_missing_approval_ledger_module_fails_the_gate_loudly_not_silently(
    tmp_path: Path,
) -> None:
    """Tier 2: the rename-witness architect asked for — if
    ``reyn.security.permissions.approval_ledger`` cannot be imported at
    all (the module renamed/moved out from under this gate), the gate
    script must fail LOUDLY (a non-zero exit from an unhandled
    ``ImportError``), never silently report ``OK`` / exit 0. Verified by
    actually running the gate script in a subprocess with a decoy
    ``reyn.security.permissions`` package (real files on disk, no
    ``approval_ledger`` module inside it) placed first on ``sys.path`` —
    a real import failure, not a simulated one."""
    decoy_root = tmp_path / "decoy_reyn_pkg"
    pkg_dir = decoy_root / "reyn" / "security" / "permissions"
    pkg_dir.mkdir(parents=True)
    (decoy_root / "reyn" / "__init__.py").write_text("", encoding="utf-8")
    (decoy_root / "reyn" / "security" / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    # Deliberately NO approval_ledger.py in the decoy package — this is
    # the "renamed/moved" scenario: the import the gate script's own
    # module-level line depends on has nothing to resolve to.

    repo_root = REPO_ROOT
    gate_script = repo_root / "scripts" / "check_approval_ledger_import_boundary.py"

    result = subprocess.run(
        [sys.executable, str(gate_script)],
        cwd=repo_root,
        env={
            "PATH": "/usr/bin:/bin",
            # decoy_root FIRST so its incomplete reyn.security.permissions
            # package shadows the real one for this subprocess only.
            "PYTHONPATH": f"{decoy_root}{os.pathsep}{repo_root / 'src'}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0, (
        "a missing approval_ledger module must fail the gate script "
        f"loudly, not exit 0 — got returncode={result.returncode}, "
        f"stdout={result.stdout!r}"
    )
    assert "approval_ledger" in result.stderr, (
        f"expected an ImportError naming approval_ledger, got: {result.stderr!r}"
    )


def test_the_real_approval_ledger_module_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current file (not assumed), matching the sibling gates' own
    "run it before shipping it" discipline. #5173 measured this by hand
    (approval_ledger.py has zero reyn-internal imports); this asserts it
    stayed that way."""
    from scripts.check_approval_ledger_import_boundary import _APPROVAL_LEDGER_PATH

    offenders = reyn_internal_imports(_APPROVAL_LEDGER_PATH)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
