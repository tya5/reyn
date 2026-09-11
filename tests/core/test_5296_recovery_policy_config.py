"""Tier 1: #5296 recovery-policy configuration contract."""
from __future__ import annotations

import logging

import pytest

from reyn.config.chat import CompactionConfig, _build_chat_config


def test_fold_persist_policy_defaults_to_next_turn() -> None:
    """Tier 1: the default preserves compaction for the next turn."""
    assert CompactionConfig().fold_persist_policy == "next_turn"


def test_fold_persist_policy_parses_never_and_next_turn() -> None:
    """Tier 1: both declared stop-lines parse from chat.compaction."""
    for policy in ("never", "next_turn"):
        cfg = _build_chat_config({"compaction": {"fold_persist_policy": policy}})
        assert cfg.compaction.fold_persist_policy == policy


def test_recovery_policy_alias_warns_and_preserves_value(caplog) -> None:
    """Tier 1: the old key warns once while retaining its value. Was
    `pytest.warns(DeprecationWarning, ...)` before #6143 -- that emission
    is SILENT outside `__main__` under Python's own default filter, so
    the notice is now `logger.warning`, read here via `caplog`."""
    with caplog.at_level(logging.WARNING):
        cfg = _build_chat_config({"compaction": {"recovery_policy": "never"}})
    assert any("recovery_policy" in r.message for r in caplog.records)
    assert cfg.compaction.fold_persist_policy == "never"


def test_fold_persist_policy_rejects_unknown_values() -> None:
    """Tier 1: an undeclared stop-line fails at construction."""
    with pytest.raises(ValueError, match="fold_persist_policy"):
        CompactionConfig(fold_persist_policy="aggressive")
