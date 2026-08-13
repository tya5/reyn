"""Tier 1: #4494 design C — the Artifacts drawer pane's ref-table-fallback
source disclosure (``chrome.py``). Lead-coder's own ruling, verbatim:
"C ＋ 明示。文面は「件数」ではなく「情報源の限界」を言うこと" — the
disclosure states the SOURCE LIMITATION, never a row count, and is
appended REGARDLESS of whether the fallback found anything (the falsify
requirement: emptying the ref table empties the rows but the disclosure
text stays).
"""
from __future__ import annotations

from reyn.core.present.artifact_list import ArtifactRow
from reyn.interfaces.inline.textual_chat.chrome import (
    ARTIFACT_REF_TABLE_FALLBACK_DISCLOSURE,
    artifact_pane_commands,
    artifact_pane_options,
)

_ROW = ArtifactRow(
    ref="r1", name="report.pdf", media_type=None, description=None, is_inline=False,
)


def test_live_source_never_appends_the_disclosure():
    """Tier 1: the default (live conversation-derived) source is
    unaffected by this PR — no disclosure row for a client whose own
    conversation state is a complete answer."""
    rows = artifact_pane_options([_ROW])
    assert ARTIFACT_REF_TABLE_FALLBACK_DISCLOSURE not in rows


def test_fallback_source_appends_the_disclosure_after_a_populated_list():
    """Tier 1: a non-empty fallback list still gets the disclosure row
    appended after the real rows."""
    rows = artifact_pane_options([_ROW], source="ref_table_fallback")
    assert rows == ["report.pdf", ARTIFACT_REF_TABLE_FALLBACK_DISCLOSURE]


def test_falsify_emptying_the_fallback_still_shows_the_disclosure():
    """Tier 1: (falsify) an empty ref-table fallback must not read as a
    plain "no artifacts" — the reader needs to know WHY it might be
    empty (a genuinely-empty table vs. an inline-only agent whose
    artifacts the table never records)."""
    rows = artifact_pane_options([], source="ref_table_fallback")
    assert rows == ["(no artifacts yet)", ARTIFACT_REF_TABLE_FALLBACK_DISCLOSURE]


def test_disclosure_never_names_a_count():
    """Tier 1: lead-coder's ruling forbids a count claim (a fallback
    genuinely cannot count what it has no record of) — assert the
    literal text carries no digit."""
    assert not any(ch.isdigit() for ch in ARTIFACT_REF_TABLE_FALLBACK_DISCLOSURE)


def test_fallback_commands_stay_index_aligned_with_the_disclosure_row():
    """Tier 1: the disclosure row itself is never actionable — the
    command list must carry one extra empty string so
    ``on_option_list_option_selected``'s index lookup never targets the
    disclosure row with a stale command."""
    options = artifact_pane_options([_ROW], source="ref_table_fallback")
    commands = artifact_pane_commands([_ROW], source="ref_table_fallback")
    assert len(options) == len(commands)
    assert commands[-1] == ""
