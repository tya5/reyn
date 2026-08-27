"""Tier 1: #5296 recovery-policy configuration contract."""
from __future__ import annotations

import pytest

from reyn.config.chat import CompactionConfig, _build_chat_config


def test_recovery_policy_defaults_to_same_turn() -> None:
    """Tier 1: the default permits same-turn recovery."""
    assert CompactionConfig().recovery_policy == "same_turn"


def test_recovery_policy_parses_never_and_same_turn() -> None:
    """Tier 1: both declared stop-lines parse from chat.compaction."""
    for policy in ("never", "same_turn"):
        cfg = _build_chat_config({"compaction": {"recovery_policy": policy}})
        assert cfg.compaction.recovery_policy == policy


def test_recovery_policy_rejects_unknown_values() -> None:
    """Tier 1: an undeclared stop-line fails at construction."""
    with pytest.raises(ValueError, match="recovery_policy"):
        CompactionConfig(recovery_policy="aggressive")
