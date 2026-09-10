"""Tier 2: #5990 (part of, stage 2) — `Session._build_retrieval_bundle`'s
own `except Exception: embedding_provider = None; ...` swallowed a
provider/index construction failure (missing dependency or malformed
`embedding:` config) with no visible report. Now warns before falling
through to "no search_actions this session", same convention #6038
established.

Real Session construction (`make_session`, the same helper 280+ other
tests use) throughout -- only the ONE collaborator whose failure is
under test (`ActionEmbeddingIndex`) is monkeypatched to raise, on its
own real module attribute (the exact seam `session.py`'s own local
import reads at call time).
"""
from __future__ import annotations

import logging

from reyn.config.embedding import EmbeddingConfig
from tests._support.agent_session import make_session

_LOGGER_NAME = "reyn.runtime.session"


def test_action_index_construction_failure_warns_and_degrades_to_no_index(
    tmp_path, monkeypatch, caplog,
) -> None:
    """Tier 2: `ActionEmbeddingIndex` raising during a REAL Session's own
    construction warns AND the session still comes up with no
    search_actions (read via the public `RouterHostAdapter` surface,
    the SAME method `RouterLoop.run()` itself calls every turn) rather
    than failing to start.

    Strip-falsify (verified by hand: the `logger.warning(...)` call
    removed from `_build_retrieval_bundle`'s `except Exception`): this
    test goes red — no record at all (the degrade itself still happens
    either way, so the return-value assertion alone is not a witness —
    matching test-review Q3's own "was handed X is not used X" shape)."""
    import reyn.tools.action_index as action_index_module

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated action-index construction failure")

    monkeypatch.setattr(action_index_module, "ActionEmbeddingIndex", _raise)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        session = make_session(
            agent_name="test-agent-5990-stage2",
            workspace_base_dir=tmp_path,
            embedding_config=EmbeddingConfig(enabled=True),
        )

    router_host = session._router_host
    idx = router_host.get_action_embedding_index()
    assert idx is None
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("action-embedding provider/index" in r.message for r in records)


def test_well_formed_embedding_config_does_not_warn(tmp_path, caplog) -> None:
    """Tier 2: negative control -- a real Session with embedding.enabled
    left at its own default (off) never reaches the construction branch
    at all, so no warning fires."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        make_session(
            agent_name="test-agent-5990-stage2-control",
            workspace_base_dir=tmp_path,
        )
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
