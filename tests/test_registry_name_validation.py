"""Tier 2: _validate_agent_name contract — registry.py.

`_validate_agent_name(name)` enforces the agent-name alphabet:
1-32 chars of `[a-z0-9_-]`, first char restricted to `[a-z0-9]`.
Raises ValueError on any violation; returns None on success.
"""
from __future__ import annotations

import pytest

from reyn.runtime.registry import _validate_agent_name

# ── valid names ───────────────────────────────────────────────────────────────


def test_validate_agent_name_simple_lowercase() -> None:
    """Tier 2: all-lowercase name passes without raising."""
    _validate_agent_name("alice")


def test_validate_agent_name_single_char_letter() -> None:
    """Tier 2: single lowercase letter is the minimum valid name."""
    _validate_agent_name("a")


def test_validate_agent_name_starts_with_digit() -> None:
    """Tier 2: digit is a valid first character."""
    _validate_agent_name("0agent")


def test_validate_agent_name_with_hyphen() -> None:
    """Tier 2: hyphens are allowed in non-leading positions."""
    _validate_agent_name("agent-01")


def test_validate_agent_name_with_underscore() -> None:
    """Tier 2: underscores are allowed in non-leading positions."""
    _validate_agent_name("a_b_c")


def test_validate_agent_name_max_length() -> None:
    """Tier 2: 32-character name is at the allowed maximum."""
    _validate_agent_name("a" * 32)


# ── invalid names ─────────────────────────────────────────────────────────────


def test_validate_agent_name_empty_raises() -> None:
    """Tier 2: empty string raises ValueError."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("")


def test_validate_agent_name_leading_underscore_raises() -> None:
    """Tier 2: underscore in first position raises ValueError."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("_bad")


def test_validate_agent_name_leading_hyphen_raises() -> None:
    """Tier 2: hyphen in first position raises ValueError."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("-bad")


def test_validate_agent_name_uppercase_raises() -> None:
    """Tier 2: uppercase letters are not in the allowed alphabet."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("Agent")


def test_validate_agent_name_over_max_length_raises() -> None:
    """Tier 2: 33-character name exceeds the 32-char limit."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("a" * 33)


def test_validate_agent_name_space_raises() -> None:
    """Tier 2: space character is not in the allowed alphabet."""
    with pytest.raises(ValueError, match="invalid agent name"):
        _validate_agent_name("has space")


def test_validate_agent_name_returns_none_on_success() -> None:
    """Tier 2: valid name returns None (no value, no exception)."""
    result = _validate_agent_name("valid-name-01")
    assert result is None
