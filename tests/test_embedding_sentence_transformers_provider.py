"""Tier 2: FP-0043 Phase 2 — sentence-transformers backend env / config contract.

Covers the env / cache / device resolution + class-name lookup +
graceful failure modes WITHOUT requiring the `sentence_transformers`
library itself. The real embedding path (= encode() against a loaded
model) is reachable only when `pip install 'reyn[local-embed]'` has been
performed and is covered by manual smoke testing + the Phase 1 bench
runner.

What lands here:
  1. Cache directory precedence: REYN_CACHE_DIR > XDG_CACHE_HOME > default
  2. Device resolution from REYN_EMBED_DEVICE (= cpu/mps/cuda + fallback)
  3. Class-name → model-string resolution via configured classes
  4. estimate_tokens (= char/4 heuristic, no API call)
  5. ImportError carries the canonical install hint when the library is
     absent at .embed() time.
"""
from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

import pytest

from reyn.config import EmbeddingClassSpec, EmbeddingConfig
from reyn.embedding.sentence_transformers_provider import (
    SentenceTransformersEmbeddingProvider,
    _resolve_cache_dir,
    _resolve_device,
    _strip_prefix,
)


def _config_with_local_class() -> EmbeddingConfig:
    return EmbeddingConfig(
        default_class="local-mini",
        classes={
            "local-mini": EmbeddingClassSpec(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
            "standard": EmbeddingClassSpec(
                model="openai/text-embedding-3-small",
            ),
        },
        batch_size=100,
        max_concurrent_batches=1,
        max_retries=3,
        retry_backoff="exponential",
        tokenizer="cl100k_base",
    )


# ── 1. Cache directory precedence ───────────────────────────────────────────


def test_cache_dir_uses_reyn_cache_dir_first(tmp_path, monkeypatch) -> None:
    """Tier 2: REYN_CACHE_DIR explicit override wins (lead-coder stance)."""
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path / "reyn-cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert _resolve_cache_dir() == tmp_path / "reyn-cache" / "sentence-transformers"


def test_cache_dir_falls_back_to_xdg_when_reyn_absent(tmp_path, monkeypatch) -> None:
    """Tier 2: XDG_CACHE_HOME backstop (tui-coder stance) when REYN_CACHE_DIR unset."""
    monkeypatch.delenv("REYN_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert _resolve_cache_dir() == tmp_path / "xdg" / "reyn" / "sentence-transformers"


def test_cache_dir_falls_back_to_home_default(tmp_path, monkeypatch) -> None:
    """Tier 2: ~/.cache/reyn/ final default when both env vars unset."""
    monkeypatch.delenv("REYN_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    expected = Path.home() / ".cache" / "reyn" / "sentence-transformers"
    assert _resolve_cache_dir() == expected


# ── 2. Device resolution ────────────────────────────────────────────────────


@pytest.mark.parametrize("device", ["cpu", "mps", "cuda"])
def test_device_valid_values_pass_through(monkeypatch, device: str) -> None:
    """Tier 2: REYN_EMBED_DEVICE accepts cpu / mps / cuda verbatim."""
    monkeypatch.setenv("REYN_EMBED_DEVICE", device)
    assert _resolve_device() == device


def test_device_defaults_to_cpu_when_unset(monkeypatch) -> None:
    """Tier 2: no env → cpu (= FP-0043 "no GPU auto-detection" default)."""
    monkeypatch.delenv("REYN_EMBED_DEVICE", raising=False)
    assert _resolve_device() == "cpu"


def test_device_invalid_value_warns_and_falls_back_to_cpu(monkeypatch) -> None:
    """Tier 2: typo / unknown device → warn + cpu fallback (= no hard fail)."""
    monkeypatch.setenv("REYN_EMBED_DEVICE", "tpu")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _resolve_device()
    assert result == "cpu"
    assert any("REYN_EMBED_DEVICE" in str(w.message) for w in caught), (
        "invalid REYN_EMBED_DEVICE must emit a UserWarning"
    )


# ── 3. Class-name resolution ────────────────────────────────────────────────


def test_class_name_resolves_to_model_string() -> None:
    """Tier 2: 'local-mini' → 'sentence-transformers/all-MiniLM-L6-v2' via classes map."""
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)
    assert p.resolve_model("local-mini") == "sentence-transformers/all-MiniLM-L6-v2"


def test_model_string_with_slash_passes_through() -> None:
    """Tier 2: a fully-qualified model string is returned verbatim."""
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)
    assert (
        p.resolve_model("sentence-transformers/all-MiniLM-L6-v2")
        == "sentence-transformers/all-MiniLM-L6-v2"
    )


def test_strip_prefix_drops_sentence_transformers_namespace() -> None:
    """Tier 2: _strip_prefix removes the routing prefix for HF id lookup."""
    assert _strip_prefix("sentence-transformers/all-MiniLM-L6-v2") == "all-MiniLM-L6-v2"
    assert (
        _strip_prefix("sentence-transformers/intfloat/multilingual-e5-small")
        == "intfloat/multilingual-e5-small"
    )
    # Non-prefixed string passes through unchanged.
    assert _strip_prefix("all-MiniLM-L6-v2") == "all-MiniLM-L6-v2"


# ── 4. Token estimation ─────────────────────────────────────────────────────


def test_estimate_tokens_uses_char_heuristic() -> None:
    """Tier 2: estimate_tokens uses char/4 fallback (= local has no API cost)."""
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)
    texts = ["hello world", "hi", ""]
    # 11//4 + 2//4 + 0//4 = 2 + 0 + 0 = 2
    assert p.estimate_tokens(texts) == 2


def test_estimate_tokens_empty_list_returns_zero() -> None:
    """Tier 2: empty input returns 0 tokens; no API call would happen."""
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)
    assert p.estimate_tokens([]) == 0


# ── 5. ImportError carries install hint ─────────────────────────────────────


def test_embed_raises_import_error_with_install_hint(monkeypatch) -> None:
    """Tier 2: missing optional dep → ImportError pointing at the extras command.

    The visibility gate elsewhere catches this and keeps search_actions
    hidden; the hint message is the user-facing onboarding cue.
    """
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)

    # Force the import to fail even if the lib is locally installed.
    for k in list(sys.modules):
        if k.startswith("sentence_transformers"):
            monkeypatch.delitem(sys.modules, k, raising=False)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(ImportError) as excinfo:
        asyncio.run(p.embed(["hello"], "local-mini"))
    assert "reyn[local-embed]" in str(excinfo.value)


# ── 6. Empty input short-circuits without lazy-loading the model ────────────


def test_embed_empty_input_does_not_trigger_lazy_load(monkeypatch) -> None:
    """Tier 2: empty texts return empty vectors without touching sentence_transformers.

    Keeps the "zero-cost when unused" promise (= cold-start latency stays
    low) when an upstream caller hands us an empty batch.
    """
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)

    # Block the import; if _load were reached, this would raise.
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    result = asyncio.run(p.embed([], "local-mini"))
    assert result["vectors"] == []
    assert result["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert result["total_tokens"] == 0
