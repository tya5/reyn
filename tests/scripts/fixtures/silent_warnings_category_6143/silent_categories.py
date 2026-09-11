"""Fixture for test_silent_warnings_category_gate_6143.py -- NOT scanned by
the real gate (only `src/reyn/**` is in scope); read directly by the tests
via `silent_category_sites()`.

Deliberately holds one call per silent-by-default category, plus a
keyword-form call, so the detector's category resolution is exercised both
positionally and by keyword.
"""
from __future__ import annotations

import warnings


def positional_deprecation() -> None:
    warnings.warn("old thing", DeprecationWarning, stacklevel=2)


def positional_pending_deprecation() -> None:
    warnings.warn("will be deprecated", PendingDeprecationWarning, stacklevel=2)


def positional_import() -> None:
    warnings.warn("import shim", ImportWarning, stacklevel=2)


def positional_resource() -> None:
    warnings.warn("leaked", ResourceWarning, stacklevel=2)


def keyword_deprecation() -> None:
    warnings.warn("old thing", category=DeprecationWarning, stacklevel=2)
