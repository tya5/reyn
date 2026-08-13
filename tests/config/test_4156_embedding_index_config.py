"""Tier 1/2: `embedding.index.*` — WHICH workloads `embedding.enabled: true`
turns on (#4156, architect design + lead-coder's default-value ruling).

`embedding.enabled` used to have two jobs bundled into one switch: "may
reyn call an embedding provider" AND "what does reyn embed" (action
catalog + the whole-repo knowledge index, no way to get one without the
other). An operator opting into the ~10-entry action catalog for
`search_actions` was also opting into an unconditional, repo-size-
proportional knowledge-index build — the owner hit a 5M TPM ceiling this
way. `embedding.index.actions` (default True) / `embedding.index.
repo_knowledge` (default False) split the two workloads apart.
"""
from __future__ import annotations

import pytest

from reyn.config import EmbeddingConfig, EmbeddingIndexConfig, _build_embedding_config


def test_default_index_config_is_actions_on_repo_knowledge_off():
    """Tier 1: the shipped defaults — actions stays on (negligible TPM,
    byte-identical to the pre-#4156 experience for an operator who never
    touches this field), repo_knowledge stays off (the workload that
    caused the owner's TPM incident)."""
    cfg = EmbeddingIndexConfig()
    assert cfg.actions is True
    assert cfg.repo_knowledge is False


def test_embedding_config_default_carries_the_default_index_config():
    """Tier 1: ReynConfig's default-constructed EmbeddingConfig carries a
    default-constructed EmbeddingIndexConfig, not None or a bare dict."""
    cfg = EmbeddingConfig()
    assert isinstance(cfg.index, EmbeddingIndexConfig)
    assert cfg.index.actions is True
    assert cfg.index.repo_knowledge is False


def test_parser_omitted_index_block_keeps_defaults():
    """Tier 2: omitting `index:` entirely keeps both defaults."""
    cfg = _build_embedding_config({"enabled": True})
    assert cfg.index.actions is True
    assert cfg.index.repo_knowledge is False


def test_parser_explicit_repo_knowledge_opt_in():
    """Tier 2: an operator explicitly opting into the repo-knowledge
    workload gets it — the field is read, not ignored."""
    cfg = _build_embedding_config({
        "enabled": True, "index": {"repo_knowledge": True},
    })
    assert cfg.index.repo_knowledge is True
    # actions stays at its own default when not mentioned — the two
    # fields are independent, not coupled to each other.
    assert cfg.index.actions is True


def test_parser_explicit_actions_opt_out():
    """Tier 2: an operator can turn OFF the action catalog too (e.g. to
    save cost while still wanting repo-knowledge search) — non-default
    read-through, the reverse direction from the previous test."""
    cfg = _build_embedding_config({
        "enabled": True, "index": {"actions": False},
    })
    assert cfg.index.actions is False
    assert cfg.index.repo_knowledge is False


def test_parser_both_index_fields_together():
    """Tier 2: both fields set explicitly in the same block round-trip
    independently."""
    cfg = _build_embedding_config({
        "enabled": True,
        "index": {"actions": False, "repo_knowledge": True},
    })
    assert cfg.index.actions is False
    assert cfg.index.repo_knowledge is True


def test_parser_index_actions_non_bool_raises():
    """Tier 2: a malformed value raises — same discipline as every other
    typed field in this parser (a mistyped value must not silently no-op
    or silently coerce)."""
    with pytest.raises(ValueError, match="embedding.index.actions"):
        _build_embedding_config({"index": {"actions": "yes"}})


def test_parser_index_repo_knowledge_non_bool_raises():
    """Tier 2: same discipline, the other field."""
    with pytest.raises(ValueError, match="embedding.index.repo_knowledge"):
        _build_embedding_config({"index": {"repo_knowledge": "yes"}})


def test_parser_index_non_dict_raises():
    """Tier 2: `index:` itself must be a mapping — a scalar or list is
    rejected rather than silently ignored."""
    with pytest.raises(ValueError, match="embedding.index"):
        _build_embedding_config({"index": "not-a-dict"})


def test_parser_malformed_top_level_raw_returns_index_defaults():
    """Tier 2: not a dict at all (e.g. a bare string in reyn.yaml) →
    defaults, index included."""
    cfg = _build_embedding_config("not-a-dict")
    assert cfg.index == EmbeddingIndexConfig()
