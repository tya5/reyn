"""Tier 1: every committed LLM replay fixture holds zero stacked-generation
groups (#3634).

A fixture "stacks" when the SAME logical call — same ``model`` + ``tool_choice``
+ per-message digest sequence, i.e. everything ``LLMReplay.key`` hashes over
EXCEPT ``tools`` — is recorded under more than one key. This happens when a
tool's schema changes (a JSON-schema field, or ONLY its ``description`` string
— #3634 measured that a description-only edit already moves the key) and the
fixture is regenerated in place: before #3634, ``LLMReplay.flush`` only ever
appended, so the OLD entry (recorded against the old schema) survived
alongside the NEW one and the fixture then matched BOTH schema generations —
green regardless of which one the code actually implements. That is worse
than a stale fixture: a stale fixture goes RED and gets noticed; a stacked
one stays GREEN and measures nothing, and ordinary CI has no way to tell.

This gate is the structural backstop #3634 asks for: even though #3634 also
fixed ``LLMReplay.flush`` to replace instead of append (so a clean
regeneration cannot stack going forward), this test is what would catch a
FUTURE regression in that mechanism, or a stacked fixture landing by some
other path (a hand-edited fixture, a merge conflict resolved by picking both
sides), without requiring a human to notice.

Coverage caveat (measured, not assumed): grouping requires each entry's
#3473 ``key_components`` fingerprint. An entry recorded before #3473 carries
none and cannot be grouped precisely — ``replay_stacking.stacked_groups``
silently excludes it rather than guessing, so a pre-#3473 fixture that
happens to ALSO be stacked would report zero groups here. As of #3634, 32 of
the 34 committed fixture files under ``tests/fixtures/llm`` predate #3473 and
are therefore UNMEASURABLE by this gate (not "measured clean") — every
fixture recorded or re-recorded from here on carries the fingerprint, so this
gap only affects fixtures nobody has touched since #3473 landed and it
shrinks every time one of those is legitimately re-recorded.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.dev.testing.replay_stacking import has_fingerprinted_entries, stacked_groups

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "llm"
_FIXTURE_FILES = sorted(_FIXTURES_ROOT.rglob("*.jsonl"))


@pytest.mark.parametrize(
    "fixture_path",
    _FIXTURE_FILES,
    ids=[str(p.relative_to(_FIXTURES_ROOT)) for p in _FIXTURE_FILES],
)
def test_fixture_holds_no_stacked_generations(fixture_path: Path) -> None:
    """Tier 1: a committed fixture never carries the same logical call under
    more than one key.

    Only checkable for fixtures carrying at least one #3473 ``key_components``
    fingerprint — see module docstring. A fixture with none is skipped (not
    passed): a skip here means "cannot tell", a pass means "checked, clean",
    and conflating the two would let a genuinely unmeasurable, possibly-stacked
    legacy fixture read as verified.
    """
    if not has_fingerprinted_entries(fixture_path):
        pytest.skip(
            f"{fixture_path.name} predates #3473 (no key_components "
            "fingerprint on any entry) — stacking cannot be precisely "
            "detected for this file. See module docstring."
        )
    stacked = stacked_groups(fixture_path)
    assert not stacked, (
        f"{fixture_path} holds {len(stacked)} logical call(s) recorded under "
        f"more than one key (stale tool-schema generations stacked instead of "
        f"replaced — #3634): "
        + "; ".join(f"{len(set(keys))} distinct keys for one call" for keys in stacked.values())
        + ". Re-record with the fixture-owning test (delete-first is no "
        "longer required — #3634 made LLMReplay.flush replace in place)."
    )
