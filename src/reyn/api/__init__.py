"""``reyn.api`` — the top-level public-API namespace (#1783).

Groups ``safe/`` (the safe-mode allow-of-one surface, `reyn.api.safe.*`) and
``unsafe/`` (default-deny, no allowlisted members) under one real package.

This ``__init__.py`` was missing between #1700 (moved the pre-#1783
``interfaces/api/`` package, deleting its own ``__init__.py`` with it) and
#1783 (established this NEW top-level ``reyn.api`` location by moving
``safe/``/``unsafe/`` in, but never recreated a parent ``__init__.py`` for
it) — a gap, not a deliberate namespace-package choice (#1783's own commit
message describes "establishing the top-level reyn.api namespace" in the
ordinary sense of a name scope, not a PEP 420 technical decision; nothing in
that diff or #311 addresses import mechanics for the parent directory
itself). Restored here so ``src/reyn/api/`` is a real package again,
consistent with every other domain-group directory in this tree.
"""
from __future__ import annotations
