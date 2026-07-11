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
import types
import warnings
from pathlib import Path

import pytest

from reyn.config import EmbeddingClassSpec, EmbeddingConfig
from reyn.data.embedding.sentence_transformers_provider import (
    SentenceTransformersEmbeddingProvider,
    _resolve_cache_dir,
    _resolve_device,
    _resolve_offline_mode,
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


# ── 7. HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE — FP-0057 Phase 4 fast-fail opt-in ─


def _clear_offline_env(monkeypatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)


@pytest.mark.parametrize("env_var", ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"])
@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES"])
def test_offline_mode_truthy_values(monkeypatch, env_var: str, value: str) -> None:
    """Tier 2: either HF-standard offline env var, truthy, enables offline mode."""
    _clear_offline_env(monkeypatch)
    monkeypatch.setenv(env_var, value)
    assert _resolve_offline_mode() is True


@pytest.mark.parametrize("env_var", ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"])
@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_offline_mode_falsy_values(monkeypatch, env_var: str, value: str) -> None:
    """Tier 2: any non-truthy offline env value keeps offline mode off."""
    _clear_offline_env(monkeypatch)
    monkeypatch.setenv(env_var, value)
    assert _resolve_offline_mode() is False


def test_offline_mode_defaults_to_off_when_unset(monkeypatch) -> None:
    """Tier 2: neither offline env set preserves pre-Phase-4 behaviour (real load attempt)."""
    _clear_offline_env(monkeypatch)
    assert _resolve_offline_mode() is False


class _RecordingSentenceTransformer:
    """Real fake standing in for ``sentence_transformers.SentenceTransformer``.

    Records the kwargs it was constructed with so tests can assert on the
    ``local_files_only`` wiring without needing the real (heavy, network-
    capable) dependency installed. No Mock/AsyncMock per testing policy —
    this is a plain class with real attribute state.
    """

    last_kwargs: "dict | None" = None

    def __init__(self, model_id: str, **kwargs) -> None:
        _RecordingSentenceTransformer.last_kwargs = dict(kwargs)
        self._model_id = model_id

    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts, **_kwargs):
        return [[0.1, 0.2, 0.3] for _ in texts]


class _RaisingSentenceTransformer:
    """Real fake that always raises — simulates 'not in local cache'."""

    def __init__(self, model_id: str, **kwargs) -> None:
        raise OSError(
            f"Cannot find an appropriate cached snapshot folder for {model_id!r} "
            "since local_files_only=True was set."
        )


def _install_fake_st_module(monkeypatch, cls) -> None:
    """Inject a fake ``sentence_transformers`` module exposing ``cls`` as
    ``SentenceTransformer``, so ``_load`` reaches a real (non-network)
    constructor call instead of the actual heavy dependency."""
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)


def test_load_passes_local_files_only_when_offline_mode_set(monkeypatch) -> None:
    """Tier 2: HF_HUB_OFFLINE=1 -> reyn EXPLICITLY passes local_files_only=True to
    the ctor (belt-and-suspenders: does not rely on the library version internally
    honouring the env var)."""
    _clear_offline_env(monkeypatch)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _install_fake_st_module(monkeypatch, _RecordingSentenceTransformer)
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)

    asyncio.run(p.embed(["hello"], "local-mini"))

    assert _RecordingSentenceTransformer.last_kwargs is not None
    assert _RecordingSentenceTransformer.last_kwargs["local_files_only"] is True


def test_load_omits_local_files_only_when_offline_mode_unset(monkeypatch) -> None:
    """Tier 2: default (neither offline env set) never passes local_files_only —
    preserves pre-Phase-4 behaviour (a real load attempt is still made). This is
    the falsify pair: stripping the offline flag flips the ctor kwarg, so the
    test differentiates offline fast-fail from the default network attempt."""
    _clear_offline_env(monkeypatch)
    _install_fake_st_module(monkeypatch, _RecordingSentenceTransformer)
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)

    asyncio.run(p.embed(["hello"], "local-mini"))

    assert _RecordingSentenceTransformer.last_kwargs is not None
    assert "local_files_only" not in _RecordingSentenceTransformer.last_kwargs


def test_load_offline_mode_uncached_model_fails_fast_not_silently(monkeypatch) -> None:
    """Tier 2: HF_HUB_OFFLINE + uncached model -> the real exception propagates
    (graceful-degrade, not a crash-into-hang and not a silently-empty index — the
    caller, router_loop's background build, is what turns this into the
    decision-enabling operator warning; covered end-to-end in
    tests/test_action_embedding_build_failure_1458.py)."""
    _clear_offline_env(monkeypatch)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _install_fake_st_module(monkeypatch, _RaisingSentenceTransformer)
    cfg = _config_with_local_class()
    p = SentenceTransformersEmbeddingProvider(config=cfg)

    with pytest.raises(OSError, match="local_files_only"):
        asyncio.run(p.embed(["hello"], "local-mini"))
