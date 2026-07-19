# scaffold: triggered_by="#3082 Family 2 (WAL-event/recovery bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 2
extraction — ``Session._build_recovery_bundle`` pulling the
``generation_store -> journal`` (``SnapshotGenerationStore`` /
``SnapshotJournal``) sequence out of ``Session.__init__`` into one builder
returning one typed bundle (``_RecoveryBundle``).

This is a pure output->input builder pipeline stage: there is no separate
"old" implementation left in the tree to diff against (the inline sequence
was replaced, not duplicated), so byte-identical is pinned as the set of
*construction-order + wiring invariants* the inline sequence guaranteed and
the builder must reproduce exactly:

1. ``journal`` is wired to the SAME ``generation_store`` instance the builder
   returns (not a fresh one) — proven end-to-end: a generation cut through
   ``journal.cut_generation`` lands in the very ``generation_store`` object
   the caller holds, not some other store.
2. ``journal`` is wired to the LOCAL ``state_log`` __init__ parameter (never
   ``self._state_log``, a separate later tracking assignment out of scope
   for this extraction) — proven end-to-end: a WAL-recorded mutation through
   the journal durably lands in the exact ``state_log`` instance the caller
   passed to the builder.
3. ``journal`` is wired to the exact ``snapshot_path`` passed to the builder
   — proven end-to-end: ``journal.save()`` writes to that exact path.
4. ``agent_name`` / ``session_id`` pass through into the journal's in-memory
   snapshot (``journal.snapshot.agent_name`` / ``.session_id``).

Per the extracted-refactor idiom (``docs/deep-dives/contributing/testing.md``,
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold test is added and removed in the SAME PR that lands the
extraction, once green — the builder has no independent behavior left to keep
re-verifying past that point (it is a one-time mechanical extraction, not an
area that will keep changing shape).

No new WAL-derived recovery state is introduced by this extraction (pure
move of existing construction code) so the CLAUDE.md truncate-falsify
recovery-feature PR gate does not apply here; what this scaffold instead pins
is that the WAL SUBSTRATE WIRING itself (journal <-> generation_store <->
state_log) survives the extraction byte-identically, since that wiring — not
new derived state — is where this extraction's recovery-fidelity risk lives.
"""
from __future__ import annotations

import pytest

from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.session import Session, _RecoveryBundle
from tests._support.agent_session import make_session


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return make_session(agent_name="family2-recovery-bundle-test")


class TestFamily2RecoveryBundleByteIdentical:
    def test_bundle_types_and_wiring(self, session: Session) -> None:
        """Tier 1: the builder produces the same TWO real object types the
        inline sequence built, assigned to the same Session attributes."""
        is_generation_store = isinstance(session._generation_store, SnapshotGenerationStore)
        is_journal = isinstance(session.journal, SnapshotJournal)
        assert is_generation_store
        assert is_journal

    def test_builder_returns_a_recovery_bundle(self, tmp_path, monkeypatch) -> None:
        """Tier 1: calling the builder directly (public method on Session)
        returns a ``_RecoveryBundle`` with the two expected real object
        types — the builder's contract independent of ``__init__`` wiring."""
        monkeypatch.chdir(tmp_path)
        state_log = StateLog(tmp_path / "direct-builder.wal")
        # Session() itself isn't needed to invoke the (instance) builder method
        # directly — construct a minimal session only to obtain a bound method.
        s = make_session(agent_name="family2-direct-builder-test")
        bundle = s._build_recovery_bundle(
            "family2-direct-builder-test",
            tmp_path / "direct-snapshot.json",
            state_log,
            "direct-session-id",
        )
        is_bundle = isinstance(bundle, _RecoveryBundle)
        is_generation_store = isinstance(bundle.generation_store, SnapshotGenerationStore)
        is_journal = isinstance(bundle.journal, SnapshotJournal)
        assert is_bundle
        assert is_generation_store
        assert is_journal

    @pytest.mark.asyncio
    async def test_journal_cuts_generations_into_the_same_generation_store(
        self, tmp_path, monkeypatch
    ) -> None:
        """Tier 1: invariant 1 — ``journal.cut_generation`` durably records
        into THE SAME ``generation_store`` object the builder returned (and
        ``session._generation_store`` holds), not some other/fresh store.
        Proven end-to-end via the generation_store's own public ``seqs()``
        read, not a private-attribute identity peek. ``cut_generation`` is a
        no-op with no WAL (state_log=None), so this test wires a real
        ``StateLog`` — matching ``cut_generation``'s own documented
        precondition ("No-op when no generation store / WAL is configured")."""
        monkeypatch.chdir(tmp_path)
        state_log = StateLog(tmp_path / "cut-generation.wal")
        session = make_session(agent_name="family2-cut-generation-test", state_log=state_log)
        before = session._generation_store.seqs()
        await session.journal.append_inbox(kind="test", payload={"x": 1})
        await session.journal.cut_generation(anchor="probe")
        await session.journal.flush()
        after = session._generation_store.seqs()
        assert len(after) > len(before)

    @pytest.mark.asyncio
    async def test_journal_writes_wal_entries_into_the_local_state_log_param(
        self, tmp_path, monkeypatch
    ) -> None:
        """Tier 1: invariant 2 — the journal durably appends into the EXACT
        ``state_log`` instance passed to the builder (the LOCAL __init__
        parameter), proven end-to-end by observing that state_log's own
        ``current_seq`` / ``last_durable_seq`` advance, not by peeking at
        ``journal._state_log`` identity."""
        monkeypatch.chdir(tmp_path)
        state_log = StateLog(tmp_path / "wiring.wal")
        s = make_session(agent_name="family2-state-log-wiring-test", state_log=state_log)
        before = state_log.current_seq
        await s.journal.append_inbox(kind="test", payload={"y": 2})
        await state_log.flush()
        after = state_log.current_seq
        assert after > before

    @pytest.mark.asyncio
    async def test_journal_saves_to_the_exact_snapshot_path_passed_to_the_builder(
        self, tmp_path, monkeypatch
    ) -> None:
        """Tier 1: invariant 3 — ``journal.save()`` durably writes to the
        EXACT ``snapshot_path`` the builder was given (``self._snapshot_path``
        at the original inline call site), proven by reading the file back
        from that exact path — not a private ``journal._snapshot_path``
        peek."""
        monkeypatch.chdir(tmp_path)
        snap_path = tmp_path / "custom" / "snapshot.json"
        s = make_session(agent_name="family2-snapshot-path-test", snapshot_path=snap_path)
        await s.journal.save()
        assert snap_path.exists()

    def test_agent_name_and_session_id_pass_through(self, tmp_path, monkeypatch) -> None:
        """Tier 1: invariant 4 — ``agent_name`` / ``session_id`` builder
        inputs reach the journal's in-memory snapshot, proven via the
        journal's own public ``snapshot`` property."""
        monkeypatch.chdir(tmp_path)
        s = make_session(
            agent_name="family2-passthrough-test",
            session_id="family2-custom-sid",
        )
        assert s.journal.snapshot.agent_name == "family2-passthrough-test"
        assert s.journal.snapshot.session_id == "family2-custom-sid"

    @pytest.mark.asyncio
    async def test_strip_falsify_generation_store_identity_check_is_live(
        self, tmp_path, monkeypatch
    ) -> None:
        """Tier 1: strip-falsify — re-running invariant 1's check (does
        ``seqs()`` grow after a real ``cut_generation``) against a FRESH,
        never-written-to generation_store (instead of the real wired
        ``session._generation_store``) must observe NO growth, proving the
        growth observed in
        ``test_journal_cuts_generations_into_the_same_generation_store`` is
        genuinely reading the journal's real wired store, not a check that
        would pass against any store regardless of wiring (non-vacuous)."""
        monkeypatch.chdir(tmp_path)
        state_log = StateLog(tmp_path / "strip-falsify.wal")
        session = make_session(agent_name="family2-strip-falsify-test", state_log=state_log)
        fresh_store = SnapshotGenerationStore(
            "family2-strip-falsify", session._snapshot_path.parent / "fresh-generations",
        )
        before = fresh_store.seqs()
        await session.journal.append_inbox(kind="test", payload={"z": 3})
        await session.journal.cut_generation(anchor="probe-poisoned")
        await session.journal.flush()
        after_fresh = fresh_store.seqs()
        after_real = session._generation_store.seqs()
        assert after_fresh == before, (
            "strip-falsify: a cut through the real journal was observed on an "
            "unrelated fresh generation_store — the wiring check would be vacuous"
        )
        assert len(after_real) > len(before)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
