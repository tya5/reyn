"""Custom ``build_py`` hook: mirror ``docs/reference/`` into the builtin tier
before setuptools collects package-data (proposal 0060 Addendum D, D5b).

All other metadata/config stays declarative in ``pyproject.toml``
(``build-backend = "setuptools.build_meta"``) — this file exists solely to
give the build a pre-collection hook point setuptools' declarative
``pyproject.toml`` config has no equivalent for. See
``scripts/mirror_reference_docs.py`` for the copy mechanics and rationale.
"""
from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from mirror_reference_docs import mirror_reference_docs  # noqa: E402


class build_py(_build_py):
    def run(self) -> None:
        mirror_reference_docs()
        super().run()


setup(cmdclass={"build_py": build_py})
