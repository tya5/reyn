"""Tier 1: scripts/test_tier_audit.py refuses a target set that resolves to nothing.

CLAUDE.md instructs every author to run the audit as ``--strict <changed test
files>`` before opening a PR, so the argument list is hand-typed or
shell-expanded: a typo (``test/`` for ``tests/``), a path another PR has since
moved, or an empty shell variable all reach ``main()`` as targets that resolve
to zero files. Until #4577 that printed "No test files found." and returned 0,
which authors then recorded in a Test plan as a passed gate — the same class as
#4576's mypy-absent ratchet green, in a different script: a measurement that
did not happen, reported in the colour of one that did.

Tier 1 because the audit script is the contract surface every tests-touching PR
is checked through; its exit code is what a human reads as "the gate passed."

Public surface only (no mocks — the script is stdlib-only and its ``main`` is
directly callable): real ``main(argv)``, real path resolution, real filesystem.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "test_tier_audit.py"


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` without pytest collecting it.

    Same loader idiom as the sibling ``test_tier_audit_format_pin.py``: the
    script's filename starts with ``test_``, and ``@dataclass`` needs the
    module registered in ``sys.modules`` before exec.
    """
    spec = importlib.util.spec_from_file_location("_audit_empty_targets_4577", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_targets_resolving_to_zero_files_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """Tier 1: a target set that names no test file at all exits non-zero.

    The consumer is the PR author following CLAUDE.md's pre-push instruction:
    before #4577 a mistyped path here produced exit 0, and the gate's own
    output ("No test files found.") scrolled past as if it were a result.
    """
    audit = _load_audit_module()

    missing = tmp_path / "test_this_file_does_not_exist.py"
    assert not missing.exists(), "precondition: the path must genuinely resolve to nothing"

    assert audit.main(["--strict", str(missing)]) != 0


def test_a_real_test_file_still_audits_and_passes(tmp_path: Path) -> None:
    """Tier 1: the accept side — a well-formed test file still exits 0.

    Its consumer is every author whose paths ARE correct: a refusal that fires
    on a resolvable target would block the gate's own users, which is the
    failure mode the reject test above cannot see.
    """
    audit = _load_audit_module()

    good = tmp_path / "test_well_formed.py"
    good.write_text(
        '"""Tier 2: a well-formed module for the audit to accept."""\n'
        "\n"
        "\n"
        "def test_something() -> None:\n"
        '    """Tier 2: asserts a value, pins no format, fakes no collaborator."""\n'
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    assert audit.main(["--strict", str(good)]) == 0
