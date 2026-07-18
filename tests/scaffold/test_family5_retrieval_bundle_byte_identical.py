# scaffold: triggered_by="#3082 Family 5 (retrieval bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 5
extraction — ``Session._build_retrieval_bundle`` pulling the embedding block
(``embedding_provider`` / ``embedding_event_sink`` / ``embedding_model_class``
/ ``action_embedding_index``, one conditional construction + try/except
None-fallback) and ``action_usage_tracker`` (a SEPARATE conditional + try/
except None-fallback, regrouped from Family 4 per the architect's DAG
correction) out of ``Session.__init__`` into one builder returning one typed
bundle (``_RetrievalBundle``).

★ The load-bearing distinction from Families 3/4: this family's builder is
invoked BEFORE Family 1 (``_build_audit_event_bundle``), at its ORIGINAL
inline position (~line 1152). Both closures inside it
(``_embedding_event_sink`` / ``_on_hot_list_changed``) resolve
``self._chat_events`` at CALL time, not at builder-call time — eager-izing
that reference (the Family 3/4 pattern) would crash Session construction
with ``AttributeError`` (``self._chat_events`` does not exist yet at line
~1152). This scaffold pins that deferred resolution directly: it calls the
bound builder on a Session instance that has NOT had ``self._chat_events``
assigned yet (mirroring the exact in-flight state during ``__init__``),
proving construction is crash-free, then assigns ``_chat_events`` afterward
and drives the returned closures through it — proving they re-resolve
``self._chat_events`` live, not a value snapshotted at builder-call time.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.config.embedding import ActionRetrievalConfig, EmbeddingConfig
from reyn.core.events.events import EventLog
from reyn.data.embedding.router_provider import RoutingEmbeddingProvider
from reyn.runtime.session import Session, _RetrievalBundle
from reyn.tools.action_index import ActionEmbeddingIndex
from reyn.tools.action_usage_tracker import ActionUsageTracker


@pytest.fixture
def enabled_session(tmp_path, monkeypatch) -> Session:
    """A Session with BOTH the embedding block and the action_usage_tracker
    enabled (universal_wrappers_enabled defaults True; ``embedding_class`` /
    ``hot_list_n`` opt in), so both conditionals' success branches run."""
    monkeypatch.chdir(tmp_path)
    return Session(
        agent_name="family5-retrieval-bundle-test",
        action_retrieval_config=ActionRetrievalConfig(
            universal_wrappers_enabled=True,
            embedding_class="light",  # openai/text-embedding-3-small — no ST extras needed
            hot_list_n=5,
        ),
        embedding_config=EmbeddingConfig(),
    )


@pytest.fixture
def disabled_session(tmp_path, monkeypatch) -> Session:
    """A default Session — ``embedding_class=None`` / ``hot_list_n=0`` are
    the ``ActionRetrievalConfig`` defaults, so both conditionals' guard-false
    branches run and all five attrs stay at their None default."""
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family5-retrieval-bundle-disabled-test")


class TestFamily5RetrievalBundleByteIdentical:
    # ── enabled + success ───────────────────────────────────────────────

    def test_session_holds_retrieval_attrs_of_real_type(
        self, enabled_session: Session,
    ) -> None:
        """Tier 1: the builder assigns the same real object types the inline
        sequence built, on the same five ``Session`` attributes."""
        assert isinstance(enabled_session._embedding_provider, RoutingEmbeddingProvider)
        assert isinstance(enabled_session._action_embedding_index, ActionEmbeddingIndex)
        assert enabled_session._embedding_model_class == "light"
        assert callable(enabled_session._embedding_event_sink)
        assert isinstance(enabled_session._action_usage_tracker, ActionUsageTracker)

    def test_builder_returns_a_retrieval_bundle(self, enabled_session: Session) -> None:
        """Tier 1: calling the builder directly (public method on Session)
        returns a ``_RetrievalBundle`` wrapping the real components — the
        builder's contract independent of ``__init__`` unpack wiring."""
        bundle = enabled_session._build_retrieval_bundle(
            enabled_session._action_retrieval,
            EmbeddingConfig(),
            enabled_session.agent_name,
        )
        assert isinstance(bundle, _RetrievalBundle)
        assert isinstance(bundle.embedding_provider, RoutingEmbeddingProvider)
        assert isinstance(bundle.action_embedding_index, ActionEmbeddingIndex)
        assert bundle.embedding_model_class == "light"
        assert callable(bundle.embedding_event_sink)
        assert isinstance(bundle.action_usage_tracker, ActionUsageTracker)

    def test_embedding_model_class_passthrough(self, tmp_path, monkeypatch) -> None:
        """Tier 1: ``embedding_model_class`` IS ``action_retrieval.embedding_class``
        (config passthrough, not a hardcoded string) — strip-falsify-shaped:
        a DIFFERENT configured class must show up on the bundle, proving the
        pin genuinely reads the config rather than always returning "light"."""
        monkeypatch.chdir(tmp_path)
        s = Session(
            agent_name="family5-model-class-test",
            action_retrieval_config=ActionRetrievalConfig(
                universal_wrappers_enabled=True, embedding_class="standard",
            ),
            embedding_config=EmbeddingConfig(),
        )
        assert s._embedding_model_class == "standard"

    def test_action_embedding_index_wired_to_cwd_workspace_root(
        self, enabled_session: Session, tmp_path,
    ) -> None:
        """Tier 1: ``ActionEmbeddingIndex(workspace_root=Path.cwd())`` —
        the index's cache location resolves under the session's cwd at
        construction time (proven via the index's own public ``db_path``
        property, not a private attribute peek)."""
        db_path = enabled_session._action_embedding_index.db_path
        assert db_path is not None
        assert str(db_path).startswith(str(tmp_path))

    # ── not-enabled: guard-false branch, both conditionals ──────────────

    def test_not_enabled_all_five_attrs_none(self, disabled_session: Session) -> None:
        """Tier 1: with the default ``ActionRetrievalConfig`` (embedding_class
        None, hot_list_n 0), both if-guards are False and all five attrs stay
        at their pre-conditional None default — the guard-false branch of
        BOTH conditionals in one assertion set."""
        assert disabled_session._embedding_provider is None
        assert disabled_session._action_embedding_index is None
        assert disabled_session._embedding_model_class is None
        assert disabled_session._embedding_event_sink is None
        assert disabled_session._action_usage_tracker is None

    # ── failure: embedding try/except None-fallback fires ───────────────

    def test_embedding_provider_construction_failure_falls_back_to_none(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Tier 1: the embedding block's try/except None-fallback — when the
        guard passes (embedding_class configured, no missing extras) but
        construction inside the try raises, all FOUR embedding attrs fall
        back to None (real failure, no mocking: a malformed but real
        ``embedding_config`` — a bare str lacking the ``.classes`` /
        ``.get`` surface ``LiteLLMEmbeddingProvider.__init__`` needs —
        drives an actual ``AttributeError`` inside the try)."""
        monkeypatch.chdir(tmp_path)
        s = Session(
            agent_name="family5-embedding-failure-test",
            action_retrieval_config=ActionRetrievalConfig(
                universal_wrappers_enabled=True,
                embedding_class="light",
                hot_list_n=0,  # isolate: keep action_usage_tracker out of this probe
            ),
            embedding_config="not-a-real-embedding-config",  # type: ignore[arg-type]
        )
        assert s._embedding_provider is None
        assert s._action_embedding_index is None
        assert s._embedding_model_class is None
        assert s._embedding_event_sink is None

    # ── action_usage_tracker: enabled / disabled ─────────────────────────

    def test_action_usage_tracker_enabled_when_hot_list_n_positive(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        s = Session(
            agent_name="family5-hotlist-enabled-test",
            action_retrieval_config=ActionRetrievalConfig(
                universal_wrappers_enabled=True, hot_list_n=3,
            ),
        )
        assert isinstance(s._action_usage_tracker, ActionUsageTracker)

    def test_action_usage_tracker_none_when_hot_list_n_zero(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        s = Session(
            agent_name="family5-hotlist-disabled-test",
            action_retrieval_config=ActionRetrievalConfig(
                universal_wrappers_enabled=True, hot_list_n=0,
            ),
        )
        assert s._action_usage_tracker is None

    def test_action_usage_tracker_persist_path_uses_agent_name(
        self, enabled_session: Session, tmp_path,
    ) -> None:
        """Tier 1: the tracker's persist path is
        ``.reyn/agents/<agent_name>/action_usage.json`` (the LOCAL
        ``agent_name`` __init__ parameter) — proven behaviourally: merging a
        valid qualified-name record persists a file at that exact path."""
        enabled_session._action_usage_tracker.merge_compacted(
            [("file__read", 1.0)],
        )
        expected = (
            tmp_path / ".reyn" / "agents"
            / "family5-retrieval-bundle-test" / "action_usage.json"
        )
        assert expected.exists()

    # ── ★ deferred chat_events pin (this family's crux) ──────────────────

    def test_builder_does_not_crash_before_chat_events_exists(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Tier 1 ★: the builder is invoked (in real ``__init__``) BEFORE
        ``self._chat_events`` is assigned (Family 1 runs later). This test
        reproduces that exact in-flight state — a real ``Session`` instance
        with ``_chat_events`` deliberately NOT yet set — and calls the
        bound builder directly. Construction must NOT raise. If the
        embedding/hot-list closures captured ``self._chat_events`` EAGERLY
        (the Family 3/4 pattern misapplied here), this would raise
        ``AttributeError`` at this exact call — the crash the Family 5 spec
        exists to prevent (mirror of the Family 3 :1490 incident)."""
        monkeypatch.chdir(tmp_path)
        bare = object.__new__(Session)  # real Session instance, uninitialized attrs
        assert not hasattr(bare, "_chat_events")

        bundle = Session._build_retrieval_bundle(
            bare,
            ActionRetrievalConfig(
                universal_wrappers_enabled=True,
                embedding_class="light",
                hot_list_n=3,
            ),
            EmbeddingConfig(),
            "family5-deferred-test",
        )

        assert isinstance(bundle, _RetrievalBundle)
        assert callable(bundle.embedding_event_sink)
        assert isinstance(bundle.action_usage_tracker, ActionUsageTracker)
        # still true: chat_events was never touched during construction.
        assert not hasattr(bare, "_chat_events")

    def test_embedding_event_sink_resolves_chat_events_at_call_time(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Tier 1 ★: after the crash-free builder call above, assigning
        ``self._chat_events`` AFTER THE FACT (mirroring Family 1 running
        later in ``__init__``) makes the closure start landing events on
        it — proving live, per-call resolution rather than a value
        snapshotted at builder-call time."""
        monkeypatch.chdir(tmp_path)
        bare = object.__new__(Session)
        bundle = Session._build_retrieval_bundle(
            bare,
            ActionRetrievalConfig(universal_wrappers_enabled=True, embedding_class="light"),
            EmbeddingConfig(),
            "family5-deferred-sink-test",
        )
        # Family 1 runs later in real __init__; simulate that here.
        bare._chat_events = EventLog()

        bundle.embedding_event_sink("downloading", "model x", {"pct": 10})

        types = [e.type for e in bare._chat_events.all()]
        assert "embedding_downloading" in types

    def test_hot_list_changed_closure_resolves_chat_events_at_call_time(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Tier 1 ★: same deferred-resolution proof for the SECOND closure
        (``_on_hot_list_changed``, wired to ``action_usage_tracker``) —
        the sibling to ``_embedding_event_sink`` inside this family."""
        monkeypatch.chdir(tmp_path)
        bare = object.__new__(Session)
        bundle = Session._build_retrieval_bundle(
            bare,
            ActionRetrievalConfig(universal_wrappers_enabled=True, hot_list_n=3),
            None,
            "family5-deferred-hotlist-test",
        )
        bare._chat_events = EventLog()

        bundle.action_usage_tracker.merge_compacted([("file__read", 1.0)])

        types = [e.type for e in bare._chat_events.all()]
        assert "hot_list_updated" in types

    # ── strip-falsify: deferred wiring is live, not vacuous ──────────────

    def test_strip_falsify_embedding_sink_reresolves_on_reassignment(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Tier 1: strip-falsify — after wiring ``_chat_events`` = log A and
        confirming the sink lands events on A, REASSIGN
        ``self._chat_events`` to a DIFFERENT log B and confirm subsequent
        emits land on B, not A. This proves the closure re-reads
        ``self._chat_events`` on every call (genuinely deferred) rather
        than caching the first-seen value — a check that would pass
        vacuously if the closure captured ``chat_events`` by value at
        builder-call time instead."""
        monkeypatch.chdir(tmp_path)
        bare = object.__new__(Session)
        bundle = Session._build_retrieval_bundle(
            bare,
            ActionRetrievalConfig(universal_wrappers_enabled=True, embedding_class="light"),
            EmbeddingConfig(),
            "family5-strip-falsify-test",
        )
        log_a, log_b = EventLog(), EventLog()

        bare._chat_events = log_a
        bundle.embedding_event_sink("phase_a", "x", {})
        assert "embedding_phase_a" in [e.type for e in log_a.all()]

        bare._chat_events = log_b
        bundle.embedding_event_sink("phase_b", "x", {})
        assert "embedding_phase_b" in [e.type for e in log_b.all()], (
            "strip-falsify: the closure did not re-resolve self._chat_events "
            "after reassignment — the deferred-wiring pin would be vacuous"
        )
        assert "embedding_phase_b" not in [e.type for e in log_a.all()]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
