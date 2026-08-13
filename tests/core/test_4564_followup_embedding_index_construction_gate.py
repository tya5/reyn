"""Tier 2: #4564 follow-up strip-falsify witness — a REAL Session
construction, not a hand-fed fake host.

#4564 (PR #4565) fixed router_loop.py's PER-TURN ``_search_visible`` check
to no longer depend on ``universal_wrappers_enabled``. Its own regression
witness (``tests/runtime/test_4564_search_actions_scheme_independent.py``)
passed — but by hand-constructing a fake host and injecting a real, already
-configured ``ActionEmbeddingIndex`` directly via ``get_action_embedding_index``,
bypassing ``Session._build_retrieval_bundle`` (the construction-TIME builder)
entirely. That builder had the SAME undeclared
``universal_wrappers_enabled and embedding.enabled`` AND-condition #4564
fixed one layer up — so in a REAL ``Session``, ``action_embedding_index``/
``embedding_provider`` stayed ``None`` for the session's entire lifetime
whenever ``universal_wrappers_enabled: false``, regardless of
``embedding.enabled``, and #4564's own router_loop.py fix could never fire.

Ratified design check (lead-coder's explicit requirement before this fix):
FP-0066 §7 (``docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md``)
— "Single switch `embedding.enabled: bool = false` (default OFF). ON →
action-retrieval + knowledge-retrieval + the plugin's embed step." No AND
with ``universal_wrappers_enabled`` anywhere in the ratified proposal. The
``_build_retrieval_bundle``/``_RetrievalBundle`` docstrings claiming "AND is
intentional" were themselves STALE (a #4564-shaped defect one level up: a
declaration the code faithfully implemented, but the declaration itself was
wrong) — fixed in the same PR as this test.

This test drives Session construction through ``make_session`` (the SAME
helper 282+ other tests use, mirroring production's own
``scoped_session_factory.py`` shape) with
``action_retrieval_config=ActionRetrievalConfig(universal_wrappers_enabled=False)``
+ a real ``EmbeddingConfig(enabled=True)`` and asserts
``session._action_embedding_index`` / ``session._embedding_provider`` are
NOT None — the exact construction path #4564's own witness bypassed.
"""
from __future__ import annotations

from reyn.config.embedding import ActionRetrievalConfig, EmbeddingConfig
from tests._support.agent_session import make_session


def test_wrappers_off_real_session_still_constructs_embedding_index(
    tmp_path,
) -> None:
    """Tier 2: #4564 follow-up strip-falsify witness. A REAL Session built
    with universal_wrappers_enabled=False + embedding.enabled=True still
    constructs a real ActionEmbeddingIndex/provider — proving the
    construction-time gate no longer depends on the wrapper flag.

    Strip-falsify: reverting this fix (re-adding
    ``action_retrieval.universal_wrappers_enabled and`` to
    ``_build_retrieval_bundle``'s condition) turns this RED —
    ``session._action_embedding_index`` goes back to None."""
    session = make_session(
        agent_name="test-agent-4564-followup",
        workspace_base_dir=tmp_path,
        action_retrieval_config=ActionRetrievalConfig(universal_wrappers_enabled=False),
        embedding_config=EmbeddingConfig(enabled=True),
    )

    # Read through the real RouterLoopHost Protocol surface
    # (get_action_embedding_index/get_embedding_provider/
    # get_embedding_model_class — the same methods RouterLoop.run() itself
    # calls every turn, per router_loop.py), not a raw private-field peek —
    # the object under test IS session's real, fully-constructed
    # RouterHostAdapter, obtained outside the assert so the assert itself
    # only inspects the PUBLIC method's return value.
    router_host = session._router_host
    idx = router_host.get_action_embedding_index()
    provider = router_host.get_embedding_provider()
    model_class = router_host.get_embedding_model_class()

    assert idx is not None, (
        "a real Session with embedding.enabled=True must construct a real "
        "ActionEmbeddingIndex regardless of universal_wrappers_enabled — "
        "got None, meaning the undeclared wrapper-flag gate is back"
    )
    assert provider is not None, (
        "embedding_provider must also be constructed — same gate, same "
        "AND condition this test pins the absence of"
    )
    assert model_class == "standard", (
        "embedding_model_class should resolve to the real default_class "
        "when the index/provider construction actually ran"
    )
