"""Tier 2: FP-0034 PR-3b-ii ActionRetrievalConfig + parser contract.

Tests for the new ``action_retrieval:`` config block:
  - Default config has the safe defaults (= flag off, no embedding,
    mode='default', hot_list_n=10).
  - Parser accepts each field independently, validates types, and
    raises on bad values.
  - ReynConfig.action_retrieval is populated by load_config from the
    merged yaml.
  - Unknown keys in the action_retrieval block are silently ignored
    (= forward compat with Phase 2 additions).

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

# ── 1. Default values ─────────────────────────────────────────────────────


def test_default_action_retrieval_config_is_on() -> None:
    """Tier 2: out-of-the-box config has universal wrappers ENABLED.

    PR-3b-iv flipped the default from False to True. Operators who
    want to opt out can set ``universal_wrappers_enabled: false`` in
    reyn.yaml. The remaining defaults (embedding_class / hot_list_n
    / mode) are unchanged.
    """
    cfg = ActionRetrievalConfig()
    assert cfg.universal_wrappers_enabled is True
    assert cfg.embedding_class is None
    assert cfg.hot_list_n == 10  # §D24 balanced
    assert cfg.mode == "default"
    assert cfg.hide_legacy_tools is False  # Phase 2 prep, opt-in


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


def test_parser_embedding_class_set() -> None:
    """Tier 2: setting embedding_class flows through."""
    cfg = _build_action_retrieval_config({"embedding_class": "standard"})
    assert cfg.embedding_class == "standard"


def test_parser_embedding_class_empty_string_becomes_none() -> None:
    """Tier 2: empty-string embedding_class normalises to None (§D14)."""
    cfg = _build_action_retrieval_config({"embedding_class": ""})
    assert cfg.embedding_class is None


def test_parser_embedding_class_null() -> None:
    """Tier 2: explicit null embedding_class stays None."""
    cfg = _build_action_retrieval_config({"embedding_class": None})
    assert cfg.embedding_class is None


def test_parser_hot_list_n_zero() -> None:
    """Tier 2: hot_list_n=0 (= opt-out, §D24 minimal mode) is accepted."""
    cfg = _build_action_retrieval_config({"hot_list_n": 0})
    assert cfg.hot_list_n == 0


def test_parser_hot_list_n_positive() -> None:
    """Tier 2: hot_list_n positive value flows through."""
    cfg = _build_action_retrieval_config({"hot_list_n": 20})
    assert cfg.hot_list_n == 20


def test_parser_mode_minimal() -> None:
    """Tier 2: mode='minimal' (§D24) flows through."""
    cfg = _build_action_retrieval_config({"mode": "minimal"})
    assert cfg.mode == "minimal"


def test_parser_mode_performance() -> None:
    """Tier 2: mode='performance' (§D24) flows through."""
    cfg = _build_action_retrieval_config({"mode": "performance"})
    assert cfg.mode == "performance"


def test_parser_hide_legacy_tools_true() -> None:
    """Tier 2: hide_legacy_tools=true (Phase 2 prep) flows through."""
    cfg = _build_action_retrieval_config({"hide_legacy_tools": True})
    assert cfg.hide_legacy_tools is True


def test_parser_hide_legacy_tools_false() -> None:
    """Tier 2: explicit hide_legacy_tools=false matches default."""
    cfg = _build_action_retrieval_config({"hide_legacy_tools": False})
    assert cfg.hide_legacy_tools is False


def test_parser_all_fields_at_once() -> None:
    """Tier 2: all 5 fields can be set together."""
    cfg = _build_action_retrieval_config({
        "universal_wrappers_enabled": True,
        "embedding_class": "voyage_multi",
        "hot_list_n": 15,
        "mode": "performance",
        "hide_legacy_tools": True,
    })
    assert cfg.universal_wrappers_enabled is True
    assert cfg.embedding_class == "voyage_multi"
    assert cfg.hot_list_n == 15
    assert cfg.mode == "performance"
    assert cfg.hide_legacy_tools is True


# ── 3. Parser — validation errors ─────────────────────────────────────────


def test_parser_rejects_non_dict() -> None:
    """Tier 2: non-mapping at the top level raises ValueError."""
    with pytest.raises(ValueError, match="must be a mapping"):
        _build_action_retrieval_config("not a dict")


def test_parser_rejects_non_bool_wrappers_enabled() -> None:
    """Tier 2: universal_wrappers_enabled with non-bool raises."""
    with pytest.raises(ValueError, match="universal_wrappers_enabled"):
        _build_action_retrieval_config({"universal_wrappers_enabled": "yes"})


def test_parser_rejects_non_string_embedding_class() -> None:
    """Tier 2: embedding_class with non-string raises."""
    with pytest.raises(ValueError, match="embedding_class"):
        _build_action_retrieval_config({"embedding_class": 42})


def test_parser_rejects_non_int_hot_list_n() -> None:
    """Tier 2: hot_list_n with non-int raises."""
    with pytest.raises(ValueError, match="hot_list_n"):
        _build_action_retrieval_config({"hot_list_n": "10"})


def test_parser_rejects_negative_hot_list_n() -> None:
    """Tier 2: hot_list_n < 0 raises."""
    with pytest.raises(ValueError, match=">= 0"):
        _build_action_retrieval_config({"hot_list_n": -1})


def test_parser_rejects_non_string_mode() -> None:
    """Tier 2: mode with non-string raises."""
    with pytest.raises(ValueError, match="mode"):
        _build_action_retrieval_config({"mode": 42})


def test_parser_rejects_non_bool_hide_legacy_tools() -> None:
    """Tier 2: hide_legacy_tools with non-bool raises."""
    with pytest.raises(ValueError, match="hide_legacy_tools"):
        _build_action_retrieval_config({"hide_legacy_tools": "yes"})


def test_parser_ignores_unknown_keys() -> None:
    """Tier 2: unknown keys are silently ignored (forward compat)."""
    cfg = _build_action_retrieval_config({
        "universal_wrappers_enabled": True,
        "phase2_hot_list_strategy": "freq+recency",  # future field
        "phase3_cold_start_seed": ["x", "y"],
    })
    # Recognised field still set; unknown keys did not raise
    assert cfg.universal_wrappers_enabled is True


def test_parser_rejects_bool_passed_as_hot_list_n() -> None:
    """Tier 2: True/False is rejected for hot_list_n (= bool is int subclass in Python).

    Python's bool is a subclass of int — without explicit guard a
    user accidentally passing True/False would pass through silently.
    Parser must catch this.
    """
    with pytest.raises(ValueError, match="hot_list_n"):
        _build_action_retrieval_config({"hot_list_n": True})


# ── 4. End-to-end load_config integration ─────────────────────────────────


def test_load_config_picks_up_action_retrieval_yaml(tmp_path: Path) -> None:
    """Tier 2: load_config reads action_retrieval: from reyn.yaml."""
    (tmp_path / "reyn.yaml").write_text(
        """
action_retrieval:
  universal_wrappers_enabled: true
  embedding_class: standard
  hot_list_n: 15
  mode: performance
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is True
    assert cfg.action_retrieval.embedding_class == "standard"
    assert cfg.action_retrieval.hot_list_n == 15
    assert cfg.action_retrieval.mode == "performance"


def test_load_config_without_action_retrieval_uses_defaults(tmp_path: Path) -> None:
    """Tier 2: omitting action_retrieval: keeps defaults.

    Since PR-3b-iv flipped the default, an empty config gives
    operators the universal wrappers automatically. Opt-out via
    ``universal_wrappers_enabled: false`` in reyn.yaml.
    """
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is True
    assert cfg.action_retrieval.embedding_class is None
    assert cfg.action_retrieval.hot_list_n == 10
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
