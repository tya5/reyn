"""Tier 2: FP-0034 PR-3b-ii ActionRetrievalConfig + parser contract.

Tests for the ``action_retrieval:`` config block:
  - Default config has the safe defaults (= wrappers enabled, mode='default').
  - Parser accepts each field independently, validates types, and
    raises on bad values.
  - ReynConfig.action_retrieval is populated by load_config from the
    merged yaml.
  - Unknown keys in the action_retrieval block are silently ignored
    (= forward compat with Phase 2 additions).

Note: hide_legacy_tools was removed in FP-0034 Phase 6 (wrapper-only is
now the sole path). Tests for that field have been deleted.

#3218 / FP-0066 §7 P1a: the fragmented ``action_retrieval.embedding_class``
field (on/off + which model, conflated) is retired, clean-break, no alias.
The on/off decision now lives at ``embedding.enabled: bool`` (default
False); the model-class field is the (pre-existing) ``embedding.default_class``
(default "standard"). Its tests live in ``tests/config/test_embedding_config.py``;
this file keeps only the ``action_retrieval:`` block's own fields
(``universal_wrappers_enabled`` / ``mode``).

No mocks; uses real load_config with a yaml file written to tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    ActionRetrievalConfig,
    ReynConfig,
    _build_action_retrieval_config,
    load_config,
)
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

# ── 1. Default values ─────────────────────────────────────────────────────


def test_default_action_retrieval_config_is_on() -> None:
    """Tier 2: out-of-the-box config has universal wrappers ENABLED (wrapper-only path).

    PR-3b-iv flipped universal_wrappers_enabled from False to True.
    FP-0034 Phase 6 removed hide_legacy_tools (wrapper-only is the sole path).
    """
    cfg = ActionRetrievalConfig()
    assert cfg.universal_wrappers_enabled is True
    assert cfg.mode == "default"


def test_reyn_config_carries_action_retrieval_default() -> None:
    """Tier 2: ReynConfig default-constructs with an ActionRetrievalConfig.

    Default flag is True since PR-3b-iv.
    """
    cfg = ReynConfig()
    assert isinstance(cfg.action_retrieval, ActionRetrievalConfig)
    assert cfg.action_retrieval.universal_wrappers_enabled is True


# ── 2. Parser — happy path ────────────────────────────────────────────────


def test_parser_none_returns_default() -> None:
    """Tier 2: omitted block → defaults."""
    cfg = _build_action_retrieval_config(None)
    assert cfg == ActionRetrievalConfig()


def test_parser_empty_dict_returns_default() -> None:
    """Tier 2: empty dict → defaults (no-op)."""
    cfg = _build_action_retrieval_config({})
    assert cfg == ActionRetrievalConfig()


def test_parser_universal_wrappers_enabled_true() -> None:
    """Tier 2: setting universal_wrappers_enabled True flows through."""
    cfg = _build_action_retrieval_config({"universal_wrappers_enabled": True})
    assert cfg.universal_wrappers_enabled is True


def test_parser_mode_minimal() -> None:
    """Tier 2: mode='minimal' (§D24) flows through."""
    cfg = _build_action_retrieval_config({"mode": "minimal"})
    assert cfg.mode == "minimal"


def test_parser_mode_performance() -> None:
    """Tier 2: mode='performance' (§D24) flows through."""
    cfg = _build_action_retrieval_config({"mode": "performance"})
    assert cfg.mode == "performance"


def test_parser_all_fields_at_once() -> None:
    """Tier 2: all supported fields can be set together."""
    cfg = _build_action_retrieval_config({
        "universal_wrappers_enabled": True,
        "mode": "performance",
    })
    assert cfg.universal_wrappers_enabled is True
    assert cfg.mode == "performance"


# ── 3. Parser — validation errors ─────────────────────────────────────────


def test_parser_rejects_non_dict() -> None:
    """Tier 2: non-mapping at the top level raises ValueError."""
    with pytest.raises(ValueError, match="must be a mapping"):
        _build_action_retrieval_config("not a dict")


def test_parser_rejects_non_bool_wrappers_enabled() -> None:
    """Tier 2: universal_wrappers_enabled with non-bool raises."""
    with pytest.raises(ValueError, match="universal_wrappers_enabled"):
        _build_action_retrieval_config({"universal_wrappers_enabled": "yes"})


def test_parser_rejects_non_string_mode() -> None:
    """Tier 2: mode with non-string raises."""
    with pytest.raises(ValueError, match="mode"):
        _build_action_retrieval_config({"mode": 42})


def test_parser_ignores_unknown_keys() -> None:
    """Tier 2: unknown keys are silently ignored (forward compat).

    #3218 / FP-0066 §7: ``embedding_class`` is now itself an unknown key here
    (retired, clean-break) — doubles as the regression guard that removing it
    does not raise.
    """
    cfg = _build_action_retrieval_config({
        "universal_wrappers_enabled": True,
        "embedding_class": "standard",  # retired field name — now just unknown
        "phase2_hot_list_strategy": "freq+recency",  # future field
        "phase3_cold_start_seed": ["x", "y"],
    })
    # Recognised field still set; unknown keys did not raise
    assert cfg.universal_wrappers_enabled is True


# ── 4. End-to-end load_config integration ─────────────────────────────────


def test_load_config_picks_up_action_retrieval_yaml(tmp_path: Path) -> None:
    """Tier 2: load_config reads action_retrieval: from reyn.yaml."""
    (tmp_path / "reyn.yaml").write_text(
        """
action_retrieval:
  universal_wrappers_enabled: true
  mode: performance
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is True
    assert cfg.action_retrieval.mode == "performance"


def test_load_config_without_action_retrieval_uses_defaults(tmp_path: Path) -> None:
    """Tier 2: omitting action_retrieval: keeps defaults.

    Since PR-3b-iv flipped the default, an empty config gives
    operators the universal wrappers automatically. Opt-out via
    ``universal_wrappers_enabled: false`` in reyn.yaml.
    """
    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML,
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is True
    assert cfg.action_retrieval.mode == "default"


def test_load_config_with_explicit_opt_out(tmp_path: Path) -> None:
    """Tier 2: explicit `universal_wrappers_enabled: false` opt-out flows through.

    Operators who don't want the wrappers can disable them via
    reyn.yaml. This path must keep working after the default flip.
    """
    (tmp_path / "reyn.yaml").write_text(
        """
action_retrieval:
  universal_wrappers_enabled: false
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is False
