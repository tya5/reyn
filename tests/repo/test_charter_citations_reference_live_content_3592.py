"""Tier 2: `charter.md`'s feature-map citations self-verify against live content (#3592).

`docs/concepts/architecture/charter.md`'s 8x7 grid used to cite its evidence as
`feature-map.md:NNN` line numbers. A line number is silent when it goes stale:
`docs/feature-map.md` is edited far more often than the citations referencing
it, so a line number drifts to a blank line, a table header, or an unrelated
row without ever failing anything — measured at 0/42 still correct (#3591/#3592
census, 16/42 dead outright, the remaining 26 pointing at the wrong row).

A heading anchor was considered and rejected: its granularity is the section,
so "the section still exists but the specific claim inside it is now false"
survives it exactly the way a line number survives drift — same failure shape,
different index.

So each citation is now a QUOTED, VERBATIM substring of `feature-map.md`'s
CURRENT text (`feature-map.md: "exact phrase"`) — an exact-match reference
that breaks the moment the cited content stops existing, rather than silently
pointing at whatever now occupies that line. This module is the gate that
keeps the two from drifting apart: every quote extracted from charter.md must
be a literal substring of feature-map.md's live content.

Real instances throughout: the real charter.md, the real feature-map.md.
Nothing is faked — the census IS the production doc pair.
"""
from __future__ import annotations

import re

from tests._support.paths import REPO_ROOT

_REPO = REPO_ROOT
_CHARTER = _REPO / "docs" / "concepts" / "architecture" / "charter.md"
_FEATURE_MAP = _REPO / "docs" / "feature-map.md"

_CITATION_RE = re.compile(r'feature-map\.md: "([^"]*)"')


def _extract_citations() -> list[str]:
    text = _CHARTER.read_text(encoding="utf-8")
    return _CITATION_RE.findall(text)


def test_charter_has_extractable_citations() -> None:
    """Tier 2: vacuity guard — the extractor must find citations, not silently see none.

    A regex that stops matching (a reformatted delimiter, a renamed file) must
    fail LOUD here rather than let the coverage assertion below pass vacuously
    over zero citations.
    """
    citations = _extract_citations()
    assert citations, (
        "Extracted ZERO citations from charter.md — either the citation format "
        "changed (expected `feature-map.md: \"...\"`) or the file is empty. "
        "This gate is meaningless with zero citations; fix the extractor before "
        "trusting a green result from the test below."
    )


def test_every_charter_citation_is_a_live_feature_map_substring() -> None:
    """Tier 2: every quoted citation in charter.md is a literal substring of feature-map.md.

    This is the conversion's own audit: a RED here names exactly which
    citation's supporting content has moved or been removed since the
    citation was written — no separate discovery step needed.
    """
    citations = _extract_citations()
    feature_map_text = _FEATURE_MAP.read_text(encoding="utf-8")

    missing = [q for q in citations if q not in feature_map_text]

    assert not missing, (
        f"{len(missing)}/{len(citations)} charter.md citation(s) no longer "
        f"match any substring of feature-map.md (content moved, was reworded, "
        f"or was removed): {missing!r}"
    )
