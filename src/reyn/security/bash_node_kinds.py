"""#5987 stage 1 — derives the real, current set of ``tree-sitter-bash``
grammar node-kind names from the INSTALLED package, never from a hand-typed
Python list.

## Why this module exists (lead-coder's ruling on #5987)

The exec-plan parser (``exec_plan.py``) currently classifies a shell command
line by counting CHARACTERS (``_ALWAYS_FORBIDDEN_CHARS`` /
``_PUNCTUATION_CHARS``) because ``shlex`` has no grammar to allowlist nodes
OF. The ruling moves the classification UNIT to syntax-tree node kind instead
— the same unit Claude Code and Codex both use (architect's competitive
research on #5987) — specifically BECAUSE that unit's population can be
machine-DERIVED from a real grammar rather than hand-enumerated one bypass at
a time. ``tree-sitter-bash`` won the 2-discriminator ruling: wheel coverage
across reyn's declared platforms (``requires-python>=3.11``, 3.11/3.12, OS
Independent) and machine-derivable node kinds, the only candidate passing
both (``bashlex`` has no public node-kind manifest; ``Parable`` needs
CPython>=3.12, below reyn's own floor).

**This module is STAGE 1 ONLY**: it adds the derivation capability with ZERO
behavior change — :func:`derive_bash_node_kind_names` is not called from
:mod:`reyn.security.exec_plan` yet. Wiring the derived set into
``parse_exec_plan``'s actual classification (replacing
``_ALWAYS_FORBIDDEN_CHARS``/``_PUNCTUATION_CHARS`` with a node-kind
allowlist) is stage 2, a separate, later PR — lead-coder's own staging
ruling: "段1は『導出できる』ことの witness で、段2は『導出したものを使う』
ことの witness です。1本にすると、赤が出たときに『依存が入らない』のか
『分類が変わって既存が落ちた』のか分かりません."

## Why this reads ``tree_sitter.Language``'s API, NOT a ``node-types.json``
file (a documented deviation from the dispatch's stated premise)

The pre-ruling research that counted "184 node kinds" fetched
``src/node-types.json`` directly from the ``tree-sitter-bash`` GitHub
repository's source tree at the matching tag (verified again for this stage:
``curl`` against ``raw.githubusercontent.com/tree-sitter/tree-sitter-bash/
v0.25.1/src/node-types.json`` returns a 184-entry JSON array). That file is
**not shipped in the installed package** — verified directly for this stage
by installing ``tree-sitter-bash`` into a clean venv and inspecting its
contents: neither the wheel (``tree_sitter_bash-*.whl``) nor the sdist
(``tree_sitter_bash-*.tar.gz``) contains a ``node-types.json`` anywhere; the
installed package ships only ``__init__.py``/``__init__.pyi``, a compiled
``_binding.abi3.so``, and a ``queries/highlights.scm``. Reading a file this
package does not ship is not a "find the right path" problem — there is no
path.

What the installed package DOES ship, and expose via a real Python API, is
the compiled grammar itself: ``tree_sitter_bash.language()`` returns a
capsule; wrapping it in ``tree_sitter.Language(...)`` (the binding
dependency) exposes ``.node_kind_count`` and ``.node_kind_for_id(i)`` —
exactly the same symbol table ``node-types.json`` was generated FROM at build
time, read back from the compiled artifact that actually ships and actually
parses at runtime, rather than a separate JSON file that would need its own
provenance check every time the dependency is bumped. This module derives
from THAT API. The resulting population (236 unique node-kind strings across
280 symbol ids in tree-sitter-bash 0.25.1 — ratcheted by
``scripts/bash_node_kind_count_ratchet.py``, see
:data:`EXPECTED_NODE_KIND_COUNT_RATCHET`) differs from ``node-types.json``'s
184 because the two are structurally different artifacts: ``node-types.json``
also lists ABSTRACT supertype nodes (``_statement``, ``_expression``,
``_primary_expression``) that the parser's symbol table does not assign a
concrete id to, and groups some literal tokens differently — not an
discrepancy in EITHER source, just two different questions ("what grammar
rules exist" vs. "what kind-id can a produced node actually carry").

## Fail-closed guard (lead-coder's explicit instruction)

:func:`derive_bash_node_kind_names` RAISES :class:`BashNodeKindDerivationError`
if the derivation cannot run (the binding/grammar API misbehaves) or comes
back with ZERO kinds — never a silent empty set, never a fall-through. "読め
ない／導出0件なら拒否、通過ではなく."
"""
from __future__ import annotations

import functools

import tree_sitter
import tree_sitter_bash


class BashNodeKindDerivationError(RuntimeError):
    """Raised by :func:`derive_bash_node_kind_names` when the installed
    ``tree-sitter-bash``/``tree-sitter`` packages cannot produce a real,
    non-empty set of grammar node-kind names — a fail-closed guard, never a
    silent empty-set pass-through (see this module's own docstring)."""


def _load_bash_language() -> "tree_sitter.Language":
    """Wraps ``tree_sitter_bash.language()``'s capsule in a
    :class:`tree_sitter.Language` — the real, installed-package API this
    module derives node kinds from (see module docstring for why this is
    used instead of a ``node-types.json`` file, which this package does not
    ship)."""
    return tree_sitter.Language(tree_sitter_bash.language())


@functools.lru_cache(maxsize=1)
def derive_bash_node_kind_names() -> "frozenset[str]":
    """Returns the real, current set of ``tree-sitter-bash`` grammar
    node-kind name strings, derived at call time from the INSTALLED
    package's :class:`tree_sitter.Language` API (never a hand-typed Python
    list — see module docstring). Cached after the first real read
    (``functools.lru_cache``) since the installed grammar cannot change
    within one process.

    Raises :class:`BashNodeKindDerivationError` if the installed packages
    cannot produce the grammar object, or if the derived set comes back
    empty — fail-closed, per lead-coder's explicit instruction: a read
    failure or a 0-entry derivation must be refused, never silently
    treated as "no kinds to check against"."""
    try:
        language = _load_bash_language()
        kind_count = language.node_kind_count
        kinds = frozenset(
            name
            for name in (language.node_kind_for_id(i) for i in range(kind_count))
            if name
        )
    except Exception as exc:  # noqa: BLE001 -- re-raised as this module's own typed failure, never swallowed
        raise BashNodeKindDerivationError(
            "could not derive tree-sitter-bash node kinds from the "
            f"installed package ({type(exc).__name__}: {exc})"
        ) from exc
    if not kinds:
        raise BashNodeKindDerivationError(
            "tree-sitter-bash's installed grammar produced ZERO node "
            "kind names — refusing rather than passing through an empty "
            "set (a 0-entry population cannot be used as an allowlist's "
            "population)"
        )
    return kinds


#: The node-kind count derived from the CURRENTLY installed
#: ``tree-sitter-bash`` (0.25.1, verified against the actual installed
#: package for #5987 stage 1 — NOT the 184 the pre-ruling research cited,
#: which counted ``node-types.json``'s entries, a file this package does not
#: ship; see module docstring). A future dependency bump that changes the
#: grammar will change this number — ``scripts/bash_node_kind_count_ratchet.py``
#: (committed baseline + ``--write-baseline``) is the CI gate that pins it,
#: so that change is visible, not silent drift. This constant is kept only
#: as informational documentation of the value at the time this module was
#: written; the enforcement mechanism is the script's own committed
#: baseline, not this constant.
EXPECTED_NODE_KIND_COUNT_RATCHET = 236
