"""Tier 1/2: #5987 stage 1 — `tree_sitter_bash` node-kind derivation.

Covers the witnesses lead-coder's staging ruling asked stage 1 for: the
derivation reads the REAL installed package, not a hand-typed list. The
node-kind COUNT ratchet lives in `scripts/bash_node_kind_count_ratchet.py`
(committed baseline + `--write-baseline`), not here — a `tests/`-resident
count assertion duplicates a CI: gate script rather than replacing it, and
this file's own remaining test below already witnesses the population is
REAL grammar names, not merely non-empty.

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

from reyn.security.bash_node_kinds import derive_bash_node_kind_names


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
