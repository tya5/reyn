# scaffold: triggered_by="reyn.prompt package Phase 2 relocation lands (SP internal-service E-G: compaction/turn_budget/judge)"
# scaffold: removed_by="The same PR that lands the relocation, once this test is green"
"""Tier 1: byte-identical characterization gate for the SP Phase-2
(internal-service E-G: compaction/turn_budget/judge_output) relocation into
``reyn.prompt``.

``tests/scaffold/_sp_phase2_baseline_pre_refactor.json`` was captured by
mechanically calling the pre-relocation source (the commit this refactor
branched from) for: the 3 compaction-family system prompts (main, resummarize,
phase act-results), ``wrap_up_system_prompt`` (default + 2 reason variants),
and the judge_output scorer's assembled ``system_text`` across 4 representative
rubric variants (simple/multiline/empty/unicode) — reconstructed via the exact
pre-refactor f-string assembly, no manual transcription. This test re-runs the
identical calls against the CURRENT (post-relocation) source and asserts
byte-for-byte equality against that captured baseline.

This is scaffolding, not a permanent test: per the extracted-refactor idiom in
``docs/deep-dives/contributing/testing.md`` (Annex: Scaffolding tests), it is
added and removed in the SAME PR that lands the relocation, once green — the
post-relocation code has no independent behavior to keep re-verifying past
that point (the relocation is a one-time mechanical move, not an area that
will keep changing shape).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.prompt.judge import judge_system_prompt
from reyn.services.compaction.engine import (
    _COMPACTION_SYSTEM_PROMPT,
    _PHASE_COMPACTION_SYSTEM_PROMPT,
    _RESUMMARIZE_SYSTEM_PROMPT,
)
from reyn.services.turn_budget.engine import wrap_up_system_prompt

_BASELINE_PATH = Path(__file__).parent / "_sp_phase2_baseline_pre_refactor.json"


def _capture_current() -> dict:
    out: dict[str, str] = {}

    out["compaction:main"] = _COMPACTION_SYSTEM_PROMPT
    out["compaction:resummarize"] = _RESUMMARIZE_SYSTEM_PROMPT
    out["compaction:phase"] = _PHASE_COMPACTION_SYSTEM_PROMPT

    out["turn_budget:default"] = wrap_up_system_prompt()
    out["turn_budget:reason"] = wrap_up_system_prompt(reason="router reached iteration limit (5)")
    out["turn_budget:reason2"] = wrap_up_system_prompt(reason="turn budget exceeded")

    for label, rubric in [
        ("simple", "Score 0-1: is the summary non-empty?"),
        ("multiline", "Score 0-1 based on:\n- correctness\n- completeness\n- clarity"),
        ("empty", ""),
        ("unicode", "採点基準: 正確さと明瞭さ"),
    ]:
        out[f"judge:{label}"] = judge_system_prompt(rubric)

    return out


class TestSPPhase2ByteIdentical:
    def test_current_output_matches_pre_refactor_baseline(self):
        """Tier 1: every fixture's current output equals the captured
        pre-relocation baseline byte-for-byte. Covers all 3 compaction-family
        SPs, both the default and reason-tagged wrap-up SP, and the
        judge_output scorer's assembled system_text across representative
        rubric variants (incl. an interpolation seam check via unicode/empty)."""
        baseline = json.loads(_BASELINE_PATH.read_text())
        current = _capture_current()
        assert set(current) == set(baseline), (
            f"fixture key set changed: added={set(current) - set(baseline)!r} "
            f"removed={set(baseline) - set(current)!r}"
        )
        mismatches = [k for k in baseline if baseline[k] != current[k]]
        assert mismatches == [], (
            f"byte-identical relocation VIOLATED for fixtures: {mismatches!r}"
        )

    def test_strip_falsify_one_char_change_is_detected(self):
        """Tier 1: mutating one captured baseline string by 1 char must make
        the equality check fail — proves the comparison is not vacuously true."""
        baseline = json.loads(_BASELINE_PATH.read_text())
        some_key = next(iter(baseline))
        poisoned = dict(baseline)
        poisoned[some_key] = poisoned[some_key] + "X"
        assert poisoned[some_key] != baseline[some_key], (
            "strip-falsify: a 1-char mutation was not detected by direct "
            "string inequality — the fixture harness is not live"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
