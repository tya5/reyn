"""Tier 1: every committed LLM replay fixture holds zero stacked-generation
groups, ACROSS EVERY KIND a fixture can hold (#3634, generalised by #3969).

A fixture "stacks" when the SAME logical entry — for a completion, the same
``model`` + ``tool_choice`` + per-message digest sequence (everything
``LLMReplay.key`` hashes over EXCEPT ``tools``); for an environment snapshot,
the same precondition ``name`` — is recorded more than once. This happens
when a fixture is regenerated in place and the mechanism that is supposed to
replace the stale on-disk copy doesn't recognise it: before #3634,
``LLMReplay.flush`` only ever appended, so a tool-schema change left the OLD
completion entry on disk right next to the NEW one, and the fixture then
matched BOTH schema generations — green regardless of which one the code
actually implements, which is worse than a stale fixture (a stale one goes
RED and gets noticed).

#3634 fixed the completion case and gated it with a test that checked ONLY
that one kind — so when "environment" entries (introduced by #3473, keyed by
precondition ``name`` rather than a call ``key``) hit the identical failure
mode, #3634's own dedup code and its own CI gate both missed it: every
re-recording session appended a fresh "environment" line, unboundedly,
and this gate stayed green throughout (5 passed) because it never looked at
that kind (#3969, found by tui-coder re-recording #3967's fixture).

This is what #3969's own review names as the real defect: not "one more
missing check" but a gate whose declared population ("no stacking") was
actually scoped to one kind, silently. The fix here is structural, not a
second hand-added check: :mod:`reyn.dev.testing.replay_stacking` now defines
``STACKING_CHECKS`` (kind -> checker) and ``_KINDS_WITH_NO_STACKING_CONCEPT``
(kind -> explicitly exempt, with a stated reason) as the single source of
"every kind this module knows how to check, or knows it doesn't need to".
The test below enumerates the kinds ACTUALLY PRESENT in each fixture (not a
hardcoded list) and requires every one of them to appear in one of those two
sets — a THIRD kind added later that fits neither fails this gate LOUDLY,
naming the unrecognised kind, rather than silently passing through the way
"environment" did.

Coverage caveat for the ``completion`` kind specifically (measured, not
assumed): its stacking check requires each entry's #3473 ``key_components``
fingerprint. An entry recorded before #3473 carries none and cannot be
grouped precisely — see ``replay_stacking.has_fingerprinted_entries``. A
fixture whose ONLY present kind(s) are unmeasurable this way, with no other
kind reporting real stacking, SKIPS (not passes) for that fixture — the
existing #3645/pre-#3473 population this behavior was already established
for. The ``environment`` kind carries no such caveat: identity is ``name``,
always precisely checkable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.dev.testing.replay_stacking import (
    _KINDS_WITH_NO_STACKING_CONCEPT,
    STACKING_CHECKS,
    STACKING_MEASURABILITY,
    all_entry_kinds,
)

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "llm"
_FIXTURE_FILES = sorted(_FIXTURES_ROOT.rglob("*.jsonl"))


@pytest.mark.parametrize(
    "fixture_path",
    _FIXTURE_FILES,
    ids=[str(p.relative_to(_FIXTURES_ROOT)) for p in _FIXTURE_FILES],
)
def test_fixture_holds_no_stacked_generations(fixture_path: Path) -> None:
    """Tier 1: a committed fixture never carries the same logical entry —
    completion OR environment OR any future kind — recorded more than once.

    Every kind ACTUALLY PRESENT in this fixture must be either checked
    (``STACKING_CHECKS``) or explicitly exempt with a stated reason
    (``_KINDS_WITH_NO_STACKING_CONCEPT``) — an unrecognised kind FAILS this
    test by name, it is never silently skipped (that silent-skip is exactly
    how the "environment" kind went unchecked under #3634's original,
    completion-only gate — #3969).
    """
    kinds = all_entry_kinds(fixture_path)
    unrecognised = kinds - set(STACKING_CHECKS) - _KINDS_WITH_NO_STACKING_CONCEPT
    assert not unrecognised, (
        f"{fixture_path} holds entries of kind(s) {sorted(unrecognised)}, which "
        "replay_stacking.py does not yet know how to check for stacking (not in "
        "STACKING_CHECKS) or declare exempt (not in "
        "_KINDS_WITH_NO_STACKING_CONCEPT). Add one or the other before this "
        "fixture can be verified — #3969's whole point is that a new kind must "
        "be handled explicitly, not silently pass through unchecked."
    )

    checkable_kinds = kinds & set(STACKING_CHECKS)
    if not checkable_kinds:
        pytest.skip(
            f"{fixture_path.name} holds only kind(s) with no stacking concept "
            f"({sorted(kinds & _KINDS_WITH_NO_STACKING_CONCEPT)}) — nothing to check."
        )

    all_stacked: dict[str, object] = {}
    any_measured = False
    skip_reasons: list[str] = []
    for kind in sorted(checkable_kinds):
        measurable = STACKING_MEASURABILITY.get(kind)
        if measurable is not None and not measurable(fixture_path):
            skip_reasons.append(
                f"{kind}: predates the fingerprint this kind's check requires"
            )
            continue
        any_measured = True
        stacked = STACKING_CHECKS[kind](fixture_path)
        if stacked:
            all_stacked[kind] = stacked

    assert not all_stacked, (
        f"{fixture_path} holds stacked (multi-generation) entries: "
        + "; ".join(f"{kind}: {stacked!r}" for kind, stacked in all_stacked.items())
        + ". Re-record with the fixture-owning test (delete-first is no longer "
        "required — LLMReplay.flush replaces in place, #3634/#3969)."
    )
    if not any_measured:
        pytest.skip(
            f"{fixture_path.name}: no present kind is precisely measurable for "
            "stacking (" + "; ".join(skip_reasons) + ") — see module docstring."
        )
