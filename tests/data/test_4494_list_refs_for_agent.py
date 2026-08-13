"""Tier 2: #4494 design C — ``list_refs_for_agent``, the durable artifact-ref
table's own read-back for a single agent's scope. This is the source a
REMOTE client (and a LOCAL client right after a restart, #4584's own
measured finding) falls back to when its live conversation view carries
nothing.

**#4601**: the same join point also caps the fallback (newest-first) and
reports the pre-cap total, closing the "both in-repo callers read an
UNBOUNDED, ever-growing table" defect (the table is append-only and
persist-tier, #4584 — it never shrinks on its own).

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

    entries, total = list_refs_for_agent(tmp_path, "alpha")

    assert [e["ref"] for e in entries] == [ref2, ref1]
    assert entries[0]["path"] == str(f2)
    assert entries[1]["path"] == str(f1)
    assert total == 2


def test_excludes_entries_minted_under_a_different_agent(tmp_path):
    """Tier 2: scope is per-agent (architect's #4482 ruling, carried over
    here) — another agent's ref never leaks into this agent's fallback
    list."""
    f = tmp_path / "shared_name.pdf"
    f.write_text("x")
    mint_ref(tmp_path, "beta", f)

    entries, total = list_refs_for_agent(tmp_path, "alpha")
    assert entries == []
    assert total == 0


def test_empty_table_returns_empty_list(tmp_path):
    """Tier 2: (accept-side) no table on disk yet -> ([], 0), not a crash
    — the same graceful-empty contract ``resolve_ref`` already gives a
    missing table."""
    assert list_refs_for_agent(tmp_path, "alpha") == ([], 0)


def test_limit_truncates_to_the_newest_n_entries_and_reports_the_full_total(
    tmp_path,
):
    """Tier 2: (#4601) ``limit`` caps the RETURNED entries to the N
    newest, but ``total`` still names the FULL matching count — a
    caller can disclose "newest N of M" without a second, uncapped
    query."""
    paths = [tmp_path / f"f{i}.pdf" for i in range(5)]
    for p in paths:
        p.write_text("x")
    refs = [mint_ref(tmp_path, "alpha", p) for p in paths]

    entries, total = list_refs_for_agent(tmp_path, "alpha", limit=2)

    # Newest-first: the LAST two minted (refs[4], refs[3]) come back.
    assert [e["ref"] for e in entries] == [refs[4], refs[3]]
    assert total == 5


def test_limit_none_is_unbounded_same_as_omitting_it(tmp_path):
    """Tier 2: (accept-side) ``limit=None`` behaves identically to the
    default (no cap) — existing internal callers that don't pass
    ``limit`` at all keep their unbounded read."""
    f = tmp_path / "a.pdf"
    f.write_text("x")
    mint_ref(tmp_path, "alpha", f)

    with_none = list_refs_for_agent(tmp_path, "alpha", limit=None)
    without_arg = list_refs_for_agent(tmp_path, "alpha")
    assert with_none == without_arg


def test_limit_larger_than_available_entries_returns_everything(tmp_path):
    """Tier 2: (accept-side) a limit bigger than the actual population
    is a no-op cap, not an error or a padded list."""
    f = tmp_path / "a.pdf"
    f.write_text("x")
    ref = mint_ref(tmp_path, "alpha", f)

    entries, total = list_refs_for_agent(tmp_path, "alpha", limit=1000)
    assert entries == [{"ref": ref, "path": str(f)}]
    assert total == 1
