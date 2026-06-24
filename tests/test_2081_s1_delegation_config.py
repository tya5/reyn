"""Tier 1: #2081 S1 — the ``delegation:`` config field (config-selectable policy).

S1 adds ``delegation.capability_default`` (inherit|deny, default=inherit). S1 is
INERT — nothing consumes the value yet (S2 wires the unbound-delegate fallback). So
this slice only pins the contract: parse / default / validate / load_config round-trip.

The default (``inherit``) keeps a fresh install byte-identical to pre-#2081.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.infra import DelegationConfig, _build_delegation_config
from reyn.config.loader import load_config

# ── the builder contract (parse / default / validate) ───────────────────────


def test_default_is_inherit() -> None:
    """Tier 1: the default policy is ``inherit`` (byte-identical to pre-#2081)."""
    assert DelegationConfig().capability_default == "inherit"


def test_none_and_empty_default_to_inherit() -> None:
    """Tier 1: a missing block / empty dict / absent key → the default (inherit)."""
    assert _build_delegation_config(None).capability_default == "inherit"
    assert _build_delegation_config({}).capability_default == "inherit"
    assert _build_delegation_config({"other": "x"}).capability_default == "inherit"


def test_parses_inherit_and_deny() -> None:
    """Tier 1: both valid values parse."""
    assert _build_delegation_config({"capability_default": "inherit"}).capability_default == "inherit"
    assert _build_delegation_config({"capability_default": "deny"}).capability_default == "deny"


def test_invalid_value_rejected() -> None:
    """Tier 1: an out-of-domain value is rejected (decision-enabling message)."""
    with pytest.raises(ValueError, match="capability_default must be 'inherit' or 'deny'"):
        _build_delegation_config({"capability_default": "allow"})


def test_non_mapping_rejected() -> None:
    """Tier 1: a non-mapping ``delegation:`` block is rejected."""
    with pytest.raises(ValueError, match="delegation must be a mapping"):
        _build_delegation_config(["not", "a", "dict"])


def test_non_string_value_rejected() -> None:
    """Tier 1: a non-string ``capability_default`` is rejected."""
    with pytest.raises(ValueError, match="must be a string"):
        _build_delegation_config({"capability_default": 1})


# ── end-to-end through load_config (round-trip a NON-default value) ──────────


def test_load_config_round_trips_deny(tmp_path: Path) -> None:
    """Tier 1: a reyn.yaml ``delegation.capability_default: deny`` reaches
    ReynConfig.delegation (a NON-default value, so the field is genuinely wired —
    not a trivially-passing default round-trip)."""
    (tmp_path / "reyn.yaml").write_text(
        "delegation:\n  capability_default: deny\n", encoding="utf-8",
    )
    cfg = load_config(cwd=tmp_path)
    assert cfg.delegation.capability_default == "deny"


def test_load_config_absent_block_is_inherit(tmp_path: Path) -> None:
    """Tier 1: no ``delegation:`` block → the default (inherit)."""
    (tmp_path / "reyn.yaml").write_text("agent:\n  id: x\n", encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.delegation.capability_default == "inherit"
