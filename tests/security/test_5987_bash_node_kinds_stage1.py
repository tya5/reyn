"""Tier 1/2: #5987 stage 1 — `tree_sitter_bash` node-kind derivation.

Covers the witnesses lead-coder's staging ruling asked stage 1 for: the
derivation reads the REAL installed package, not a hand-typed list — the
ratchet below pins the count it actually produces, so a dependency bump that
changes the grammar is visible rather than silent drift (module docstring's
own "a future dependency bump... will change this number" disclosure).

This stage wires NOTHING into `parse_exec_plan`'s classification — a
SEPARATE pre-existing file, `test_5838_exec_plan_parser.py`, is the
before/after witness that this PR leaves it unaffected (run identically
before and after, per the dispatch's own item 5).

**Disclosed gap** (Tier review rule 4): `derive_bash_node_kind_names`'s two
`raise BashNodeKindDerivationError(...)` branches (a load failure; a 0-entry
derivation) are NOT exercised here by an automated test. Forcing either
branch needs either a genuinely broken/absent installed package (not
reproducible inside a passing test run) or faking the
`tree_sitter.Language` collaborator with a patch/hand-rolled stand-in — both
forbidden by this repo's testing policy ("no MagicMock/AsyncMock/patch, no
hand-rolled stand-in"). The guard itself is a single, inspectable
`if not kinds: raise` plus a `try/except Exception: raise ... from exc` —
reviewed by reading, not by a test that would have had to fake its way to
green.
"""
from __future__ import annotations

from reyn.security.bash_node_kinds import (
    EXPECTED_NODE_KIND_COUNT_RATCHET,
    derive_bash_node_kind_names,
)


def test_derived_kind_count_matches_ratchet() -> None:
    """Tier 1: the REAL installed `tree-sitter-bash` grammar currently
    produces EXACTLY `EXPECTED_NODE_KIND_COUNT_RATCHET` distinct node-kind
    name strings. This is a RATCHET, not an algorithm-level pin on OUR OWN
    code — the population comes from a third-party dependency's compiled
    grammar (module docstring: "a future dependency bump... will change
    this number"); a red here means either the dependency changed (update
    the ratchet deliberately, in the same PR that bumps the pin) or this
    module's derivation logic broke (do not update the ratchet for that)."""
    kinds = derive_bash_node_kind_names()
    assert len(kinds) == EXPECTED_NODE_KIND_COUNT_RATCHET


def test_derived_kinds_are_real_grammar_node_names() -> None:
    """Tier 1: the derived set contains real bash grammar node-kind names
    (not merely a non-empty set of SOMETHING) — spot-checks a handful of
    node kinds tree-sitter-bash's own grammar is known to define (command
    substitution, pipelines, redirects — the exact constructs #5838's
    character-based parser also reasons about), witnessing this reads the
    real grammar rather than an unrelated or stubbed source."""
    kinds = derive_bash_node_kind_names()
    for expected in ("program", "command", "pipeline", "command_substitution"):
        assert expected in kinds


def test_derivation_is_cached_not_reimported_per_call() -> None:
    """Tier 2: repeated calls return the SAME frozenset object (identity,
    not just equality) — witnesses the `functools.lru_cache` the module
    docstring promises ("cached after the first real read"), not a
    coincidentally-equal fresh derivation each call."""
    first = derive_bash_node_kind_names()
    second = derive_bash_node_kind_names()
    assert first is second
