"""Tier 1: `audit_event_firing_condition_gate.py`'s own detector logic
(#6071).

lead-coder's ruling: this gate flags a closed-vocabulary event kind that
has an individual `events.md` row but no firing-condition marker phrase
anywhere in that row's text — never a kind with no row at all (out of
scope, a different gap), never a correctness check on the phrase's own
accuracy.
"""
from __future__ import annotations

from scripts.audit_event_firing_condition_gate import (
    kind_rows,
    new_findings,
)


def test_a_row_naming_a_firing_condition_is_not_flagged():
    """Tier 1: the false-accept-side witness — a row containing one of
    the marker phrases (`fires`) is not a finding."""
    text = "| `session_started` | Fires once per session, at construction. |\n"
    rows = kind_rows(text, frozenset({"session_started"}))
    assert rows == {"session_started": [" Fires once per session, at construction. |"]}


def test_a_row_with_only_destination_and_payload_is_flagged():
    """Tier 1: the false-reject-side witness, required alongside the test
    above so a detector that flags nothing (which would ALSO pass the
    marker test) cannot pass this suite — a row naming only WHAT the
    payload carries, never WHEN the event fires, must be a finding."""
    text = "| `llm_called` | `model` (+ `chain_id` when the call belongs to a chain) |\n"
    vocabulary = frozenset({"llm_called"})
    rows = kind_rows(text, vocabulary)
    assert "llm_called" in rows
    findings = {
        kind for kind, row_texts in rows.items()
        if not any(
            m in t.lower()
            for t in row_texts
            for m in ("fires", "only when", "once per", "warn-once", "emitted once", "triggered when", "fired when")
        )
    }
    assert findings == {"llm_called"}


def test_a_kind_with_no_row_at_all_is_out_of_scope():
    """Tier 1: ruling's explicit scope boundary — a closed-vocabulary
    kind with NO `events.md` row at all is not counted as a finding (a
    different, already-known gap, not this gate's subject)."""
    text = "| `session_started` | Fires once per session. |\n"
    vocabulary = frozenset({"session_started", "never_documented_kind"})
    rows = kind_rows(text, vocabulary)
    assert "never_documented_kind" not in rows


def test_a_grouped_row_documents_every_kind_it_names():
    """Tier 1: the multi-kind row shape `events.md` actually uses
    (``| `a`, `b`, `c` | shared description |``) — every named kind gets
    the SAME row text, not just the first one."""
    text = "| `read_file`, `write_file`, `edit_file` | file op variants, fires per call |\n"
    vocabulary = frozenset({"read_file", "write_file", "edit_file"})
    rows = kind_rows(text, vocabulary)
    assert set(rows.keys()) == {"read_file", "write_file", "edit_file"}


def test_a_kind_documented_in_two_rows_is_not_flagged_if_either_names_a_marker():
    """Tier 1: `measured`'s own aggregation — a kind appearing in more
    than one table (rare but real) is a finding only if NONE of its rows
    name a firing condition; one marker anywhere clears it."""
    text = (
        "| `mcp_called` | destination info only, no marker here |\n"
        "| `mcp_called` | fires once per MCP tool invocation |\n"
    )
    rows = kind_rows(text, frozenset({"mcp_called"}))
    assert any("fires" in row_text for row_text in rows["mcp_called"]), (
        "the marker-bearing row must survive alongside the marker-less one"
    )
    assert any("destination info only" in row_text for row_text in rows["mcp_called"]), (
        "the marker-less row must ALSO survive -- measured() decides "
        "aggregation, kind_rows itself must not drop either"
    )


def test_measured_returns_the_scanned_count_as_documented_kinds_not_file_count(tmp_path, monkeypatch):
    """Tier 2: `measured`'s own `scanned` return is the DOCUMENTED-KIND
    count (the "97" analogue), not a file count — this gate scans exactly
    one file, so a file-count `scanned` would never legitimately read 0
    and the fail-closed check in `main` would be dead code."""
    import scripts.audit_event_firing_condition_gate as gate

    events_doc = tmp_path / "docs" / "reference" / "runtime"
    events_doc.mkdir(parents=True)
    (events_doc / "events.md").write_text(
        "| `session_started` | fires once per session |\n"
        "| `llm_called` | no marker here |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gate, "_closed_vocabulary", lambda: frozenset({"session_started", "llm_called"}),
    )

    findings, scanned = gate.measured(tmp_path)

    assert scanned == 2
    assert findings == {"llm_called"}


def test_new_findings_flags_a_finding_absent_from_baseline():
    """Tier 2: the ratchet arithmetic itself (shared shape with
    `silent_except_ratchet.py`'s own `grown_files`) — a measured finding
    not in the baseline is new debt."""
    assert new_findings({"llm_called"}, set()) == {"llm_called"}


def test_new_findings_silently_allows_a_shrink():
    """Tier 2: a baselined finding that disappears from `measured` (a
    firing-condition phrase was added, or the row was removed) is
    silently allowed to drop — the same silent-shrink contract every
    ratchet in this repo shares."""
    assert new_findings(set(), {"llm_called"}) == set()
