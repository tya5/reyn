"""Tier 2: OS invariant — `embedding:` section parsing (ADR-0033 Phase 1).

Covers:
  - Empty / missing `embedding:` section returns default EmbeddingConfig.
  - Partial config keeps defaults for unset fields.
  - str-form class resolves to EmbeddingClassSpec(model=...).
  - dict-form class with model + api_base resolves correctly.
  - ``extends`` from str base.
  - ``extends`` from dict base.
  - ``extends`` to non-existent base raises ValueError.
  - batch_size out of range raises ValueError.
  - max_concurrent_batches out of range raises ValueError.
  - max_retries out of range raises ValueError.
  - retry_backoff invalid string raises ValueError.
  - default_class not in classes raises ValueError.
  - dict form missing `model` raises ValueError.
  - Non-str / non-dict class value raises ValueError.
  - EmbeddingConfig.resolve_class() returns the correct spec.
  - EmbeddingConfig.resolve_class() raises KeyError for unknown name.
"""
from __future__ import annotations

import pytest
import yaml

from reyn.config import (
    _DEFAULT_EMBEDDING_CLASSES,
    EmbeddingClassSpec,
    EmbeddingConfig,
    _build_embedding_config,
    _parse_embedding_classes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_yaml(text: str) -> dict:
    """Parse a YAML string and return the result (always a dict or empty)."""
    result = yaml.safe_load(text)
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------


class TestDefaultEmbeddingConfig:
    def test_none_raw_returns_defaults(self):
        """Tier 2: _build_embedding_config(None) returns a fully-defaulted EmbeddingConfig."""
        cfg = _build_embedding_config(None)
        assert isinstance(cfg, EmbeddingConfig)
        assert cfg.default_class == "standard"
        assert set(cfg.classes) == {"light", "standard", "strong"}
        assert cfg.batch_size == 100
        assert cfg.max_concurrent_batches == 1
        assert cfg.max_retries == 3
        assert cfg.retry_backoff == "exponential"
        assert cfg.tokenizer == "cl100k_base"
        assert cfg.cost_warn_threshold == 10000

    def test_empty_dict_returns_defaults(self):
        """Tier 2: _build_embedding_config({}) returns a fully-defaulted EmbeddingConfig."""
        cfg = _build_embedding_config({})
        assert cfg.default_class == "standard"
        assert "light" in cfg.classes
        assert "standard" in cfg.classes
        assert "strong" in cfg.classes

    def test_non_dict_raw_returns_defaults(self):
        """Tier 2: Non-dict raw (e.g. True) falls back to defaults without raising."""
        cfg = _build_embedding_config(True)  # type: ignore[arg-type]
        assert isinstance(cfg, EmbeddingConfig)

    def test_default_class_models_match_builtin(self):
        """Tier 2: Built-in default classes have the expected model strings."""
        cfg = _build_embedding_config(None)
        assert cfg.classes["light"].model == "openai/text-embedding-3-small"
        assert cfg.classes["standard"].model == "openai/text-embedding-3-small"
        assert cfg.classes["strong"].model == "openai/text-embedding-3-large"


# ---------------------------------------------------------------------------
# Partial config
# ---------------------------------------------------------------------------


class TestPartialEmbeddingConfig:
    def test_only_batch_size_set(self):
        """Tier 2: Only batch_size overridden; all other fields stay default."""
        raw = _parse_yaml("batch_size: 50")
        cfg = _build_embedding_config(raw)
        assert cfg.batch_size == 50
        assert cfg.max_retries == 3
        assert cfg.tokenizer == "cl100k_base"
        # When no classes: key, defaults are used.
        assert "standard" in cfg.classes

    def test_only_tokenizer_set(self):
        """Tier 2: Only tokenizer overridden; all other fields stay default."""
        raw = _parse_yaml("tokenizer: p50k_base")
        cfg = _build_embedding_config(raw)
        assert cfg.tokenizer == "p50k_base"
        assert cfg.batch_size == 100


# ---------------------------------------------------------------------------
# Class parsing — str form
# ---------------------------------------------------------------------------


class TestEmbeddingClassStr:
    def test_str_class_resolves_to_spec(self):
        """Tier 2: A str class value produces EmbeddingClassSpec(model=value)."""
        result = _parse_embedding_classes({"my_class": "openai/text-embedding-3-small"})
        assert result["my_class"] == EmbeddingClassSpec(model="openai/text-embedding-3-small")

    def test_multiple_str_classes(self):
        """Tier 2: Multiple str-form entries are all resolved independently."""
        result = _parse_embedding_classes({
            "fast": "openai/text-embedding-3-small",
            "accurate": "openai/text-embedding-3-large",
        })
        assert result["fast"].model == "openai/text-embedding-3-small"
        assert result["accurate"].model == "openai/text-embedding-3-large"

    def test_str_class_has_no_api_base(self):
        """Tier 2: Str-form class has api_base=None and empty extra_body."""
        result = _parse_embedding_classes({"x": "openai/text-embedding-3-small"})
        assert result["x"].api_base is None
        assert result["x"].extra_body == {}


# ---------------------------------------------------------------------------
# Class parsing — dict form
# ---------------------------------------------------------------------------


class TestEmbeddingClassDict:
    def test_dict_with_model_and_api_base(self):
        """Tier 2: Dict class with model + api_base resolves correctly."""
        raw = {
            "custom": {
                "model": "openai/text-embedding-3-small",
                "api_base": "http://localhost:4000",
            }
        }
        result = _parse_embedding_classes(raw)
        spec = result["custom"]
        assert spec.model == "openai/text-embedding-3-small"
        assert spec.api_base == "http://localhost:4000"
        assert spec.extra_body == {}

    def test_dict_with_extra_body(self):
        """Tier 2: Dict class with extra_body is preserved."""
        raw = {
            "my": {
                "model": "openai/text-embedding-3-small",
                "extra_body": {"dimensions": 256},
            }
        }
        result = _parse_embedding_classes(raw)
        assert result["my"].extra_body == {"dimensions": 256}

    def test_dict_missing_model_raises(self):
        """Tier 2: Dict-form class without 'model' raises ValueError."""
        with pytest.raises(ValueError, match="missing the required 'model' field"):
            _parse_embedding_classes({"bad": {"api_base": "http://localhost:4000"}})

    def test_non_str_non_dict_raises(self):
        """Tier 2: Class value that is neither str nor dict raises ValueError."""
        with pytest.raises(ValueError, match="must be a str or dict"):
            _parse_embedding_classes({"bad": 42})


# ---------------------------------------------------------------------------
# Extends chain
# ---------------------------------------------------------------------------


class TestEmbeddingClassExtends:
    def test_extends_str_base(self):
        """Tier 2: Dict class extending a str-form base inherits its model."""
        raw = {
            "base": "openai/text-embedding-3-small",
            "derived": {
                "extends": "base",
                "api_base": "http://proxy:4000",
            },
        }
        result = _parse_embedding_classes(raw)
        assert result["derived"].model == "openai/text-embedding-3-small"
        assert result["derived"].api_base == "http://proxy:4000"

    def test_extends_dict_base(self):
        """Tier 2: Dict class extending a dict-form base inherits and overrides fields."""
        raw = {
            "base": {
                "model": "openai/text-embedding-3-small",
                "api_base": "http://base:4000",
                "extra_body": {"dimensions": 512},
            },
            "derived": {
                "extends": "base",
                "api_base": "http://derived:4001",
            },
        }
        result = _parse_embedding_classes(raw)
        spec = result["derived"]
        assert spec.model == "openai/text-embedding-3-small"
        assert spec.api_base == "http://derived:4001"
        # extra_body is inherited from base
        assert spec.extra_body == {"dimensions": 512}

    def test_extends_overrides_model(self):
        """Tier 2: Dict class extending a base can override the model string."""
        raw = {
            "base": "openai/text-embedding-3-small",
            "derived": {
                "extends": "base",
                "model": "openai/text-embedding-3-large",
            },
        }
        result = _parse_embedding_classes(raw)
        assert result["derived"].model == "openai/text-embedding-3-large"

    def test_extends_unknown_base_raises(self):
        """Tier 2: extends targeting a non-existent class raises ValueError."""
        raw = {
            "derived": {
                "extends": "nonexistent",
                "model": "openai/text-embedding-3-small",
            }
        }
        with pytest.raises(ValueError, match="doesn't exist"):
            _parse_embedding_classes(raw)


# ---------------------------------------------------------------------------
# Validation — range / enum checks
# ---------------------------------------------------------------------------


class TestEmbeddingConfigValidation:
    def test_batch_size_too_low_raises(self):
        """Tier 2: batch_size < 1 raises ValueError."""
        with pytest.raises(ValueError, match="batch_size"):
            _build_embedding_config({"batch_size": 0})

    def test_batch_size_too_high_raises(self):
        """Tier 2: batch_size > 2048 raises ValueError."""
        with pytest.raises(ValueError, match="batch_size"):
            _build_embedding_config({"batch_size": 2049})

    def test_batch_size_at_boundaries_ok(self):
        """Tier 2: batch_size=1 and batch_size=2048 are both valid."""
        assert _build_embedding_config({"batch_size": 1}).batch_size == 1
        assert _build_embedding_config({"batch_size": 2048}).batch_size == 2048

    def test_max_concurrent_batches_too_low_raises(self):
        """Tier 2: max_concurrent_batches < 1 raises ValueError."""
        with pytest.raises(ValueError, match="max_concurrent_batches"):
            _build_embedding_config({"max_concurrent_batches": 0})

    def test_max_concurrent_batches_too_high_raises(self):
        """Tier 2: max_concurrent_batches > 10 raises ValueError."""
        with pytest.raises(ValueError, match="max_concurrent_batches"):
            _build_embedding_config({"max_concurrent_batches": 11})

    def test_max_concurrent_batches_gt1_accepted_with_warning(self, caplog):
        """Tier 2: max_concurrent_batches > 1 is accepted but logs a warning."""
        import logging
        with caplog.at_level(logging.WARNING, logger="reyn.config"):
            cfg = _build_embedding_config({"max_concurrent_batches": 4})
        assert cfg.max_concurrent_batches == 4
        assert any("concurrent" in r.message for r in caplog.records)

    def test_max_retries_negative_raises(self):
        """Tier 2: max_retries < 0 raises ValueError."""
        with pytest.raises(ValueError, match="max_retries"):
            _build_embedding_config({"max_retries": -1})

    def test_max_retries_too_high_raises(self):
        """Tier 2: max_retries > 10 raises ValueError."""
        with pytest.raises(ValueError, match="max_retries"):
            _build_embedding_config({"max_retries": 11})

    def test_max_retries_zero_ok(self):
        """Tier 2: max_retries=0 (disable retries) is valid."""
        cfg = _build_embedding_config({"max_retries": 0})
        assert cfg.max_retries == 0

    def test_retry_backoff_invalid_raises(self):
        """Tier 2: retry_backoff not in {exponential, linear} raises ValueError."""
        with pytest.raises(ValueError, match="retry_backoff"):
            _build_embedding_config({"retry_backoff": "constant"})

    def test_retry_backoff_linear_ok(self):
        """Tier 2: retry_backoff='linear' is valid."""
        cfg = _build_embedding_config({"retry_backoff": "linear"})
        assert cfg.retry_backoff == "linear"

    def test_default_class_not_in_classes_raises(self):
        """Tier 2: default_class referring to an absent key raises ValueError."""
        raw = {
            "default_class": "missing",
            "classes": {
                "standard": "openai/text-embedding-3-small",
            },
        }
        with pytest.raises(ValueError, match="default_class"):
            _build_embedding_config(raw)

    def test_custom_classes_without_defaults(self):
        """Tier 2: User-defined classes replace defaults; default_class must match."""
        raw = {
            "default_class": "my_class",
            "classes": {
                "my_class": "openai/text-embedding-3-small",
            },
        }
        cfg = _build_embedding_config(raw)
        assert cfg.default_class == "my_class"
        assert set(cfg.classes) == {"my_class"}

    def test_empty_classes_falls_back_to_defaults(self):
        """Tier 2: classes: {} or absent causes default 3 classes to be used."""
        raw = {"classes": {}}
        cfg = _build_embedding_config(raw)
        assert set(cfg.classes) == {"light", "standard", "strong"}


# ---------------------------------------------------------------------------
# EmbeddingConfig public API
# ---------------------------------------------------------------------------


class TestEmbeddingConfigAPI:
    def test_resolve_class_known(self):
        """Tier 2: resolve_class() returns the correct EmbeddingClassSpec."""
        cfg = _build_embedding_config(None)
        spec = cfg.resolve_class("strong")
        assert spec.model == "openai/text-embedding-3-large"

    def test_resolve_class_unknown_raises_key_error(self):
        """Tier 2: resolve_class() raises KeyError for an unknown class name."""
        cfg = _build_embedding_config(None)
        with pytest.raises(KeyError):
            cfg.resolve_class("nonexistent")

    def test_full_yaml_round_trip(self):
        """Tier 2: A full embedding: block parsed via YAML is accepted and correct."""
        text = """
embedding:
  default_class: strong
  classes:
    light:    openai/text-embedding-3-small
    standard: openai/text-embedding-3-small
    strong:   openai/text-embedding-3-large
  batch_size: 200
  max_concurrent_batches: 1
  max_retries: 5
  retry_backoff: linear
  tokenizer: p50k_base
  cost_warn_threshold: 5000
"""
        parsed = _parse_yaml(text)
        cfg = _build_embedding_config(parsed.get("embedding"))
        assert cfg.default_class == "strong"
        assert cfg.batch_size == 200
        assert cfg.max_retries == 5
        assert cfg.retry_backoff == "linear"
        assert cfg.tokenizer == "p50k_base"
        assert cfg.cost_warn_threshold == 5000
        assert cfg.classes["strong"].model == "openai/text-embedding-3-large"


# ---------------------------------------------------------------------------
# _DEFAULT_EMBEDDING_CLASSES is not mutated by parse calls
# ---------------------------------------------------------------------------


class TestDefaultImmutability:
    def test_default_classes_not_mutated(self):
        """Tier 2: Calling _build_embedding_config multiple times leaves the module-level
        _DEFAULT_EMBEDDING_CLASSES dict unchanged."""
        original_keys = set(_DEFAULT_EMBEDDING_CLASSES)
        _build_embedding_config(None)
        _build_embedding_config({})
        _build_embedding_config({"batch_size": 50})
        assert set(_DEFAULT_EMBEDDING_CLASSES) == original_keys
