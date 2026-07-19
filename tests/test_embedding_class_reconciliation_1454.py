"""Tier 2: #1454 (c)+(d) — dangling embedding_class reconciliation.

A class-typed field is closed-world: ``action_retrieval.embedding_class`` must
name an entry in ``embedding.classes``. When it doesn't — the builtin
``local-mini`` default surviving after the user REPLACED ``embedding.classes``
(the owner-reported HF-blocked-company case), or a typo — the alias can never
resolve. ``_reconcile_embedding_class`` degrades semantic search to off (None)
rather than letting the dangling alias reach the embedding backend (where it
surfaces as a misleading "model not found" naming the alias).

Real config dataclasses, no mocks.
"""
from __future__ import annotations

from reyn.config import (
    ActionRetrievalConfig,
    EmbeddingClassSpec,
    EmbeddingConfig,
    ReynConfig,
    _reconcile_embedding_class,
)


def _cfg(*, embedding_class: str | None, classes: dict) -> ReynConfig:
    return ReynConfig(
        embedding=EmbeddingConfig(classes=classes),
        action_retrieval=ActionRetrievalConfig(embedding_class=embedding_class),
    )


def test_dangling_default_class_degrades_to_none():
    """Tier 2: #1454 — the builtin 'local-mini' default with NO entry in
    user-replaced embedding.classes degrades to None (search off), not error."""
    cfg = _cfg(
        embedding_class="local-mini",  # the un-overridden default
        classes={"company-proxy": EmbeddingClassSpec(model="openai/internal")},
    )
    assert cfg.action_retrieval.embedding_class == "local-mini"
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class is None


def test_explicit_dangling_class_degrades_to_none():
    """Tier 2: #1454 — an explicit class (typo) with no entry also degrades to
    None (closed-world: non-membership → graceful degrade, not crash)."""
    cfg = _cfg(
        embedding_class="standrad",  # typo for 'standard'
        classes={"standard": EmbeddingClassSpec(model="openai/text-embedding-3-small")},
    )
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class is None


def test_valid_member_class_is_unchanged():
    """Tier 2: #1454 — a class that IS in embedding.classes is left intact."""
    cfg = _cfg(
        embedding_class="standard",
        classes={"standard": EmbeddingClassSpec(model="openai/text-embedding-3-small")},
    )
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class == "standard"


def test_none_class_is_noop():
    """Tier 2: #1454 — embedding_class already None (opt-out) stays None."""
    cfg = _cfg(embedding_class=None, classes={})
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class is None


def test_default_classes_keep_standard_resolvable():
    """Tier 2: #1454 — an operator who opts IN to a builtin class (registry
    intact, which includes 'standard') is NOT degraded: 'standard' resolves
    normally.

    Since the semantic-search-opt-in fix (2026), ``ReynConfig()`` defaults
    ``embedding_class`` to None (off) rather than a truthy default — so this
    test explicitly opts in via ``ActionRetrievalConfig(embedding_class=...)``
    rather than relying on the zero-config default, to isolate the
    reconciliation behavior (builtin classes registry membership) from the
    separate opt-in-off default-value concern. (#3128 removed the
    sentence-transformers-backed 'local-mini' / 'local-e5' builtin classes
    this test previously opted into; 'standard' — litellm/openai-backed,
    unaffected by that removal — exercises the same reconciliation path.)
    """
    cfg = ReynConfig(action_retrieval=ActionRetrievalConfig(embedding_class="standard"))
    assert cfg.action_retrieval.embedding_class == "standard"
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class == "standard"


def test_zero_config_default_is_off_and_reconciliation_is_noop():
    """Tier 2: the semantic-search-opt-in fix (2026) — a true zero-config
    ``ReynConfig()`` has embedding_class=None, and reconciliation leaves it
    untouched (None is always a no-dangling-alias no-op; see
    ``test_none_class_is_noop``). This pins the NEW default distinctly from
    the reconciliation-behavior test above."""
    cfg = ReynConfig()
    assert cfg.action_retrieval.embedding_class is None
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class is None
