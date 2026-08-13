"""Tier 1: #4494 design C — the Artifacts drawer pane's ref-table-fallback
source disclosure (``chrome.py``). Lead-coder's own ruling, verbatim:
"C ＋ 明示。文面は「件数」ではなく「情報源の限界」を言うこと" — the
disclosure states the SOURCE LIMITATION, appended REGARDLESS of whether
the fallback found anything (the falsify requirement: emptying the ref
table empties the rows but the disclosure text stays).

**#4601 (lead-coder/architect ruling)**: the disclosure is now
CONSOLIDATED into one sentence with the #4599 gap architect found (a ref
whose FILE was deleted after recording still appears here but can't
open) and the new "newest N of M" truncation notice — a deliberate,
narrow exception to "never a count": ``total`` is exactly what the
fallback DOES have a record of (unlike an inline artifact's count, which
it genuinely cannot know), so stating it is not the thing the original
"never a count" ruling forbade.
"""
from __future__ import annotations

from reyn.core.present.artifact_list import ArtifactRow
from reyn.interfaces.inline.textual_chat.chrome import (
    artifact_fallback_disclosure_text,
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
    assert not any("artifact-ref table" in r for r in rows)


def test_fallback_source_appends_the_consolidated_disclosure():
    """Tier 1: a non-empty fallback list gets the consolidated disclosure
    row appended after the real rows."""
    rows = artifact_pane_options([_ROW], source="ref_table_fallback", fallback_total=1)
    assert rows == ["report.pdf", artifact_fallback_disclosure_text(1, 1)]


def test_falsify_emptying_the_fallback_still_shows_the_disclosure():
    """Tier 1: (falsify) an empty ref-table fallback must not read as a
    plain "no artifacts" — the reader needs to know WHY it might be
    empty (a genuinely-empty table vs. an inline-only agent whose
    artifacts the table never records)."""
    rows = artifact_pane_options([], source="ref_table_fallback", fallback_total=0)
    assert rows == ["(no artifacts yet)", artifact_fallback_disclosure_text(0, 0)]


def test_disclosure_names_the_inline_gap_and_the_deleted_file_gap():
    """Tier 1: (#4601) the consolidated text covers BOTH source
    limitations — an inline artifact never recorded (#4494's own gap)
    AND a recorded-but-since-deleted file that appears but can't open
    (#4599's own gap architect found unwritten anywhere)."""
    text = artifact_fallback_disclosure_text(5, 10)
    assert "inline" in text and "cannot appear" in text
    assert "deleted" in text and "cannot be opened" in text


def test_disclosure_states_the_newest_n_of_m_count():
    """Tier 1: (#4601) unlike the earlier "never a count" ruling (which
    was about a count the fallback genuinely cannot know — an inline
    artifact's count), `total` is a count the fallback DOES have a
    record of, and lead-coder's #4601 ruling explicitly requires
    disclosing it as "newest N of M"."""
    text = artifact_fallback_disclosure_text(3, 12)
    assert "newest 3 of 12" in text.lower() or "3 of 12" in text


def test_fallback_commands_stay_index_aligned_with_the_disclosure_row():
    """Tier 1: the disclosure row itself is never actionable — the
    command list must carry one extra empty string so
    ``on_option_list_option_selected``'s index lookup never targets the
    disclosure row with a stale command."""
    options = artifact_pane_options([_ROW], source="ref_table_fallback", fallback_total=1)
    commands = artifact_pane_commands([_ROW], source="ref_table_fallback")
    assert len(options) == len(commands)
    assert commands[-1] == ""
