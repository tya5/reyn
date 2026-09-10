"""Tier 2: #5939 PR-2 — `process_memory.host_swap_critical_bytes:`
parsing. Same discipline `test_5851a_process_memory_config.py` already
establishes for `max_bytes` (absent -> `None`, never a magic default;
malformed -> falls back to `None`, never coerced to 0), applied to this
THIRD, independent key.
"""
from __future__ import annotations

from reyn.config.chat import ProcessMemoryConfig, _build_process_memory_config


def test_missing_key_is_inert_by_default() -> None:
    """Tier 2: no `host_swap_critical_bytes:` key at all -> `None` (the
    knob's own inert default)."""
    cfg = _build_process_memory_config({"max_bytes": 100, "enforce": True})
    assert cfg.host_swap_critical_bytes is None


def test_valid_value_is_honored() -> None:
    """Tier 2: an explicit valid value is used verbatim."""
    cfg = _build_process_memory_config({"host_swap_critical_bytes": 500_000_000})
    assert cfg.host_swap_critical_bytes == 500_000_000


def test_zero_or_negative_falls_back_to_none_not_zero() -> None:
    """Tier 2: same discipline as `max_bytes` -- `0` (or negative) must
    not mean "critical at any swap level"; it falls back to `None`
    (inert), never coerced to the literal 0."""
    assert _build_process_memory_config({"host_swap_critical_bytes": 0}).host_swap_critical_bytes is None
    assert _build_process_memory_config(
        {"host_swap_critical_bytes": -5}
    ).host_swap_critical_bytes is None


def test_non_numeric_value_falls_back_to_none() -> None:
    """Tier 2: a malformed (non-int-coercible) value is a disclosed
    fallback, not a crash."""
    cfg = _build_process_memory_config({"host_swap_critical_bytes": "oops"})
    assert cfg.host_swap_critical_bytes is None


def test_default_construction_matches_missing_key() -> None:
    """Tier 2: the dataclass's own default equals the parser's own
    no-key answer -- one fact, not two independently-maintained ones."""
    assert ProcessMemoryConfig().host_swap_critical_bytes is None
    assert _build_process_memory_config(None).host_swap_critical_bytes is None
