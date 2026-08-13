"""Tier 2: #4156 — `embedding.index.actions` gates action-index
construction, driven through a REAL `Session`.

Mirrors `test_4564_followup_embedding_index_construction_gate.py`'s own
established pattern (a hand-fed fake host bypasses `Session._build_
retrieval_bundle`, the CONSTRUCTION-time builder — the real witness has
to go through `make_session`, the same helper 282+ other tests use,
mirroring `scoped_session_factory.py`'s own construction shape).

`embedding.index.actions` defaults True — this file's own accept-side test
(`test_index_actions_true_still_constructs_the_action_index`) is the
byte-identical-behavior witness: an operator who never touches this field
sees no change. The deny-side test is the new behavior #4156 adds.
"""
from __future__ import annotations

from reyn.config.embedding import EmbeddingConfig, EmbeddingIndexConfig
from tests._support.agent_session import make_session


def test_index_actions_false_real_session_does_not_construct_action_index(
    tmp_path,
) -> None:
    """Tier 2: a real Session built with `embedding.enabled=True` but
    `embedding.index.actions=False` must NOT construct an
    ActionEmbeddingIndex/provider — the workload-selector gate, distinct
    from the provider/cost gate.

    Strip-falsify: removing the `and embedding_config.index.actions`
    clause from `_build_retrieval_bundle`'s condition turns this RED —
    `get_action_embedding_index()` stops returning None here."""
    session = make_session(
        agent_name="test-agent-4156-actions-off",
        workspace_base_dir=tmp_path,
        embedding_config=EmbeddingConfig(
            enabled=True,
            index=EmbeddingIndexConfig(actions=False, repo_knowledge=False),
        ),
    )

    router_host = session._router_host
    idx = router_host.get_action_embedding_index()
    provider = router_host.get_embedding_provider()

    assert idx is None, (
        "embedding.index.actions=False must prevent ActionEmbeddingIndex "
        "construction even when embedding.enabled=True"
    )
    assert provider is None, (
        "embedding_provider must also stay unconstructed — same gate"
    )


def test_index_actions_true_still_constructs_the_action_index(
    tmp_path,
) -> None:
    """Tier 2: accept-side — the default (`index.actions=True`) preserves
    the pre-#4156 construction behavior: `embedding.enabled=True` alone is
    still sufficient when the operator never touches `index.actions`."""
    session = make_session(
        agent_name="test-agent-4156-actions-default",
        workspace_base_dir=tmp_path,
        embedding_config=EmbeddingConfig(enabled=True),  # index defaults actions=True
    )

    router_host = session._router_host
    idx = router_host.get_action_embedding_index()
    provider = router_host.get_embedding_provider()

    assert idx is not None, (
        "embedding.index.actions defaults True — an operator who never "
        "sets it must still get the action index when embedding.enabled=True"
    )
    assert provider is not None
