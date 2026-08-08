"""Tier 2: #3879 Stage 0 — suggest_test_dir.py's dominant-package heuristic.

Advisory-only helper (see its module docstring) — these tests are about the
computation, not about anything CI enforces."""
from __future__ import annotations

from scripts.suggest_test_dir import dominant_package


def test_no_reyn_import_returns_none() -> None:
    """Tier 2: a file with no reyn.* import at all has nothing to suggest."""
    assert dominant_package("import os\nimport sys\n") is None


def test_foundational_only_returns_none() -> None:
    """Tier 2: schemas/config/data alone don't count — a file importing ONLY
    those has no dominant SUBJECT package, by design."""
    src = "from reyn.schemas.models import Event\nfrom reyn.config import Config\n"
    assert dominant_package(src) is None


def test_single_package_wins_outright() -> None:
    """Tier 2: one non-foundational package, several imports from it."""
    src = (
        "from reyn.security.sandbox import get_default_backend\n"
        "from reyn.security.permissions import PermissionResolver\n"
    )
    assert dominant_package(src) == "security"


def test_majority_package_wins_over_a_single_foundational_import() -> None:
    """Tier 2: foundational imports present alongside a real majority must
    not dilute or distract from the real winner."""
    src = (
        "from reyn.schemas.models import Event\n"
        "from reyn.runtime.session import Session\n"
        "from reyn.runtime.registry import AgentRegistry\n"
    )
    assert dominant_package(src) == "runtime"


def test_tie_breaks_alphabetically() -> None:
    """Tier 2: equal counts must resolve the SAME way regardless of import
    order or who runs it — alphabetical, per the design."""
    src_z_first = "import reyn.tools\nimport reyn.core\n"
    src_a_first = "import reyn.core\nimport reyn.tools\n"
    assert dominant_package(src_z_first) == "core"
    assert dominant_package(src_a_first) == "core"


def test_relative_import_is_not_mistaken_for_reyn() -> None:
    """Tier 2: `from . import x` (level > 0) has no dotted module name to
    read here and must not crash or be mis-parsed as `reyn`."""
    src = "from . import helper\nfrom reyn.llm import pricing\n"
    assert dominant_package(src) == "llm"
