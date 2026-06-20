"""Shared test support helpers (fixtures, fakes, builders).

This package is the **stable** home for helpers that more than one test module
needs. Importing a helper from a sibling *test* module (``from tests.test_foo
import _bar``) is fragile: it only resolves under full-suite namespace-package
collection and breaks the moment ``test_foo.py`` is moved or collected in
isolation. Helpers here live at a location-independent import path
(``tests._support.<module>``) so neither isolated collection nor future file
moves can break them.

This is support code, **not** a test module — it contains no ``test_*``
functions and is never collected. See ``tests/README.md`` for the convention.
"""
from __future__ import annotations
