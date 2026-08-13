"""Tier 2: #4494 design C — ``list_refs_for_agent``, the durable artifact-ref
table's own read-back for a single agent's scope. This is the source a
REMOTE client (and a LOCAL client right after a restart, #4584's own
measured finding) falls back to when its live conversation view carries
nothing.

Real ``mint_ref``/table on disk throughout — no mocks.
"""
from __future__ import annotations

from reyn.data.workspace.artifact_ref import list_refs_for_agent, mint_ref


def test_returns_entries_for_the_named_agent_newest_first(tmp_path):
    """Tier 2: mirrors ``list_refs_for_agent``'s own docstring convention
    (newest-first, matching ``collect_artifact_rows``) — the LAST minted
    ref for this agent comes back first."""
    f1 = tmp_path / "a.pdf"
    f2 = tmp_path / "b.pdf"
    f1.write_text("x")
    f2.write_text("y")
    ref1 = mint_ref(tmp_path, "alpha", f1)
    ref2 = mint_ref(tmp_path, "alpha", f2)

    entries = list_refs_for_agent(tmp_path, "alpha")

    assert [e["ref"] for e in entries] == [ref2, ref1]
    assert entries[0]["path"] == str(f2)
    assert entries[1]["path"] == str(f1)


def test_excludes_entries_minted_under_a_different_agent(tmp_path):
    """Tier 2: scope is per-agent (architect's #4482 ruling, carried over
    here) — another agent's ref never leaks into this agent's fallback
    list."""
    f = tmp_path / "shared_name.pdf"
    f.write_text("x")
    mint_ref(tmp_path, "beta", f)

    assert list_refs_for_agent(tmp_path, "alpha") == []


def test_empty_table_returns_empty_list(tmp_path):
    """Tier 2: (accept-side) no table on disk yet -> [], not a crash — the
    same graceful-empty contract ``resolve_ref`` already gives a missing
    table."""
    assert list_refs_for_agent(tmp_path, "alpha") == []
