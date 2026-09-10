"""Tier 1: `audit_event_firing_condition_gate.py`'s own detector logic
(#6071).

lead-coder's ruling: this gate flags a closed-vocabulary event kind
whose `events.md` documentation is either ABSENT (``:no_row``) or
present but names no firing-condition marker phrase anywhere in its
text (``:no_marker``) — never a correctness check on a present phrase's
own accuracy. The ``:no_row`` shape was added after review (#6097): a
first draft treated a rowless kind as out of scope, which would have let
a brand-new kind land with ZERO documentation while every gate (this one
included) stayed green — the single most-wanted case unchecked.
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


def test_a_kind_with_no_row_at_all_is_flagged(tmp_path, monkeypatch):
    """Tier 1: the witness for the reviewed correction (#6097) — a
    closed-vocabulary kind with NO `events.md` row at all IS a finding,
    reported as ``:no_row`` — the case no OTHER gate in this repo checks
    (verified during review: neither "events.md" nor "AUDIT_EVENT_KINDS"
    appeared anywhere under scripts/ before this gate existed)."""
    import scripts.audit_event_firing_condition_gate as gate

    events_dir = tmp_path / "docs" / "reference" / "runtime"
    events_dir.mkdir(parents=True)
    (events_dir / "events.md").write_text(
        "| `session_started` | fires once per session |\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        gate, "_closed_vocabulary",
        lambda: frozenset({"session_started", "never_documented_kind"}),
    )

    findings, _scanned = gate.measured(tmp_path)

    assert findings == {"never_documented_kind:no_row"}


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


def test_measured_returns_the_scanned_count_as_the_full_vocabulary_size(tmp_path, monkeypatch):
    """Tier 2: `measured`'s own `scanned` return is the FULL
    closed-vocabulary size (every kind checked, documented or not), not a
    file count and not merely the documented subset — this gate scans
    exactly one file against the WHOLE vocabulary, so a file-count
    `scanned` would never legitimately read 0 and the fail-closed check
    in `main` would be dead code. Also pins the two distinct finding
    shapes together: `:no_marker` for a row with no firing phrase,
    `:no_row` for a kind with no row at all."""
    import scripts.audit_event_firing_condition_gate as gate

    events_doc = tmp_path / "docs" / "reference" / "runtime"
    events_doc.mkdir(parents=True)
    (events_doc / "events.md").write_text(
        "| `session_started` | fires once per session |\n"
        "| `llm_called` | no marker here |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gate, "_closed_vocabulary",
        lambda: frozenset({"session_started", "llm_called", "rowless_kind"}),
    )

    findings, scanned = gate.measured(tmp_path)

    assert scanned == 3
    assert findings == {"llm_called:no_marker", "rowless_kind:no_row"}


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
