"""#4206 — the 4 config axes every ``ReynConfig`` leaf is classified
under, and why the axis lives on the FIELD rather than being inferred.

Architect ruling (2026-08-11, corrected 2026-09-02T04:01 to a full
159/174-leaf classification, 要判定 0): a config key's "can a narrower
layer — agent profile / session config — override the project default"
question does NOT collapse to "permission or not" (``model`` is the
counter-example: not a capability, but a shared, bounded resource whose
free override would let a child exhaust the parent's budget). Four axes,
by DISTINCT composition rule:

- :attr:`Axis.CAPABILITY` — restrict-only (a narrower layer may only
  narrow, never widen; existing agent/session capability-profile
  machinery).
- :attr:`Axis.BOUNDING` — a narrower layer may only narrow a shared,
  bounded resource's ceiling; the operator holds the actual ceiling.
  Composition: narrowest-wins (see ``runtime/bounding.py``).
- :attr:`Axis.PREFERENCE` — free override; the last layer present wins,
  no ceiling check (see ``runtime/preferences.py``).
- :attr:`Axis.PROJECT` — no override receptacle at all; structurally
  process-shared or a placement/deployment concern (transport, storage
  directories, external registries) — NOT "unclassified", a real 4th
  answer to "who may override this" (architect: "属さない key は0").

## Why the axis lives on the dataclass FIELD

``metadata={"axis": Axis.X}`` on the field declaration, walked via
``config_schema.walk_config_schema()`` — the SAME canonical enumeration
``reyn config fields``/``get``/``set`` already use — rather than a
hand-maintained lookup table living in a 4th place. A hand list drifts
the moment a new leaf is added without updating it (the exact defect
class #4655 closed for the schema's own unknown-key detection); the
Tier 2 gate in ``tests/config/test_4206_axis_coverage.py`` asserts EVERY
real leaf carries an axis, so a new leaf added without one goes red at
review time instead of silently defaulting to "unclassified".

## ``override_enabled`` — a SEPARATE, narrower flag

Classifying a leaf's axis is NOT the same claim as "this leaf has a
live override receptacle" (``BOUNDING_KEYS``/``PREFERENCE_KEYS``,
``runtime/bounding.py``/``runtime/preferences.py``). Architect's own
explicit instruction (2026-09-02T04:01): do NOT bulk-add every
newly-bounding-classified leaf (~40) into ``BOUNDING_KEYS`` at once —
only the leaves with a MEASURED real demand for an agent/session
override get ``metadata={"axis": Axis.BOUNDING, "override_enabled":
True}``; the rest carry the axis (for the completeness gate and doc
surface) without a live receptacle yet, matching ``bounding.py``'s own
pre-existing discipline ("adding an unused key here would repeat the
exact 'declared but nobody reads it' shape #4655 closed"). Today that
is exactly the 1 pre-existing ``BOUNDING_KEYS`` member (``llm.model``)
and the 9 pre-existing ``PREFERENCE_KEYS`` members — this PR changes
WHERE that fact is declared (on the field, derived via
``walk_config_schema()``), not WHICH leaves currently have a receptacle.

## ``override_key`` — the ONE real exception found deriving ``BOUNDING_KEYS``

A leaf's own dotted ``ReynConfig`` path is not always what an operator
writes inside the override block. ``llm.model``'s ``bounding:`` entry
is the bare ``model`` — an established, tested vocabulary
(``bounding: {model: "standard"}``) that predates this leaf's own
nesting under ``llm:``, discovered live while deriving
``BOUNDING_KEYS`` (a naive derivation from ``node.key`` alone would
have silently produced ``{"llm.model"}``, breaking every real
``bounding: {model: ...}`` caller). ``field(metadata={"override_key":
"model"})`` records the override block's OWN vocabulary for this one
leaf; every other #4206 leaf's override key equals its dotted path
(the common case, no ``override_key`` needed).

## Why this module lives at ``reyn.config_axis``, not ``reyn.config.config_axis``

A genuine circular import, found live (not assumed) while migrating
``runtime/budget/budget.py``'s own ``CostLimitConfig``/``CostConfig``
dataclasses to carry axis metadata: those classes need ``Axis`` bound
at CLASS-BODY time (``field(metadata={"axis": Axis.BOUNDING})``), but
``reyn.config`` (the PACKAGE) eagerly imports ``config/chat.py``,
which imports ``CostConfig``/``CostLimitConfig`` FROM
``runtime.budget.budget`` — so ``budget.py`` importing anything from
INSIDE the ``reyn.config`` package (even a leaf submodule) at its own
module top level re-enters ``reyn/config/__init__.py`` while
``budget.py`` itself is still mid-initialization (verified directly:
``ImportError: cannot import name 'CostConfig' from partially
initialized module``). Living as its own top-level module — ``reyn/
__init__.py`` performs NO eager submodule imports (its own docstring)
— breaks the cycle structurally: importing ``reyn.config_axis`` never
touches ``reyn.config``'s own package init at all.
"""
from __future__ import annotations

from enum import StrEnum


class Axis(StrEnum):
    """The 4 config axes — see this module's own docstring."""

    CAPABILITY = "capability"
    BOUNDING = "bounding"
    PREFERENCE = "preference"
    PROJECT = "project"


__all__ = ["Axis"]
