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


def test_default_classes_keep_local_mini_resolvable():
    """Tier 2: #1454 — the zero-config default (builtin classes intact, which
    include local-mini) is NOT degraded: local-mini resolves normally."""
    cfg = ReynConfig()  # full defaults: builtin embedding.classes incl local-mini
    assert cfg.action_retrieval.embedding_class == "local-mini"
    _reconcile_embedding_class(cfg)
    assert cfg.action_retrieval.embedding_class == "local-mini"
