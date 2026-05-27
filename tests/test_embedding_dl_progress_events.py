"""Tier 2: FP-0043 Component C.3 — first-time DL progress event sink contract.

Pins the lifecycle-event surface that ``SentenceTransformersEmbeddingProvider``
emits during its lazy model load, plus the routing-wrapper forwarding
and the ``get_provider`` factory plumbing.

What is pinned:

  1. ``event_sink`` is an opt-in kwarg on
     ``SentenceTransformersEmbeddingProvider`` / ``RoutingEmbeddingProvider`` /
     ``get_provider``. When ``None`` is passed, the lazy load path is
     byte-identical to the pre-C.3 behaviour (= no events emitted).
  2. The ImportError path (= ``sentence_transformers`` not installed)
     emits a single ``("error", …, {retry_hint: …})`` event with the
     canonical install command in the hint before raising.
  3. A successful load emits exactly
        ``status`` → ``skill_done`` (in that order).
     ``status.meta`` carries ``model`` / ``target_dir`` / ``device``;
     ``skill_done.meta`` carries ``model`` / ``dimension``.
  4. A load that fails after the import succeeded (= simulated by a
     SentenceTransformer constructor raising) emits
        ``status`` → ``error``.
     ``error.meta.retry_hint`` references the ``reyn embeddings clear``
     recovery path. The original exception propagates verbatim.
  5. Sink exceptions are swallowed (= a buggy sink does NOT crash
     embedding loads).
  6. ``RoutingEmbeddingProvider`` forwards the sink to its lazily-built
     sentence-transformers backend (= the sink passed at the routing
     layer reaches the ST backend's emit path).
  7. ``get_provider("litellm", config, event_sink=…)`` passes the sink
     through to the routing wrapper.

No mocks. Tests use ``monkeypatch.setitem(sys.modules, ...)`` to install
real-shape fake ``sentence_transformers`` modules so the import + load
path runs end-to-end without requiring the actual heavy dependency.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _record() -> tuple[list[tuple[str, str, dict]], Any]:
    """Return (events_list, sink_callable) for capturing emissions."""
    events: list[tuple[str, str, dict]] = []

    def _sink(kind: str, text: str, meta: dict) -> None:
        events.append((kind, text, meta))

    return events, _sink


# ── Real-shape fake sentence_transformers module ─────────────────────────────


class _FakeST:
    """Real-shape SentenceTransformer stand-in. Constructor params match."""

    instances: list["_FakeST"] = []

    def __init__(
        self,
        hf_id: str,
        cache_folder: str | None = None,
        device: str | None = None,
        **_kw: Any,
    ):
        self.hf_id = hf_id
        self.cache_folder = cache_folder
        self.device = device
        _FakeST.instances.append(self)

    def get_sentence_embedding_dimension(self) -> int:
        return 384

    def encode(self, texts: list[str], **_kw: Any) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


class _ExplodingST:
    """SentenceTransformer constructor that always raises (= load failure path)."""

    def __init__(self, *_a: Any, **_kw: Any):
        raise RuntimeError("simulated load failure")


def _install_fake_st(monkeypatch, ctor: type) -> None:
    """Install a real-shape fake ``sentence_transformers`` module."""
    import types
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = ctor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def _uninstall_st(monkeypatch) -> None:
    """Block the import entirely (= ImportError path)."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


# ── 1. event_sink None → byte-identical pre-C.3 behaviour ────────────────────


def test_no_sink_no_emissions_on_successful_load(monkeypatch, tmp_path) -> None:
    """Tier 2: with ``event_sink=None`` the load path is silent."""
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(config={})
    result = _run(p.embed(
        ["hello"],
        "sentence-transformers/all-MiniLM-L6-v2",
    ))
    # No event_sink set → no observable side-effect besides the load.
    assert len(_FakeST.instances) == 1
    assert result["vectors"][0]  # actually loaded + ran encode


# ── 2. ImportError path emits a single error event before raising ────────────


def test_import_error_emits_error_event_with_install_hint(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: missing ``sentence_transformers`` → error event + install hint."""
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _uninstall_st(monkeypatch)

    events, sink = _record()
    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(config={}, event_sink=sink)
    with pytest.raises(ImportError) as excinfo:
        _run(p.embed(
            ["x"],
            "sentence-transformers/all-MiniLM-L6-v2",
        ))
    assert "reyn[local-embed]" in str(excinfo.value)
    # Exactly one event: error with retry_hint pointing at the extras.
    assert len(events) == 1
    kind, _text, meta = events[0]
    assert kind == "error"
    assert "reyn[local-embed]" in meta["retry_hint"]
    assert meta["model"] == "sentence-transformers/all-MiniLM-L6-v2"


# ── 3. Successful load emits status → skill_done ─────────────────────────────


def test_successful_load_emits_status_then_done(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: load lifecycle = status (with model/target_dir/device) then
    skill_done (with model/dimension), in that order.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("REYN_EMBED_DEVICE", "cpu")
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    events, sink = _record()
    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(config={}, event_sink=sink)
    _run(p.embed(
        ["hi"],
        "sentence-transformers/all-MiniLM-L6-v2",
    ))
    kinds = [k for k, _t, _m in events]
    assert kinds == ["status", "skill_done"], (
        f"expected [status, skill_done]; got {kinds}"
    )
    status_meta = events[0][2]
    done_meta = events[1][2]
    assert status_meta["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert status_meta["target_dir"]  # path string non-empty
    assert status_meta["device"] == "cpu"
    assert done_meta["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert done_meta["dimension"] == 384


def test_subsequent_load_emits_no_events_when_cached(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: second embed against an already-loaded model is silent.

    Lifecycle events fire ONLY on the cold-start lazy load; the
    in-process cache makes every subsequent call a no-op for the
    sink.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    events, sink = _record()
    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(config={}, event_sink=sink)
    _run(p.embed(["a"], "sentence-transformers/all-MiniLM-L6-v2"))
    _run(p.embed(["b"], "sentence-transformers/all-MiniLM-L6-v2"))
    # Only the first call's two events; second call hits the cache.
    assert [k for k, _t, _m in events] == ["status", "skill_done"]


# ── 4. Load failure after import emits status → error ───────────────────────


def test_load_failure_emits_status_then_error_with_clear_hint(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: SentenceTransformer constructor raising → status then error.

    ``error.meta.retry_hint`` must reference ``reyn embeddings clear``
    so the operator has a concrete recovery command when the cache
    state is partial.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _install_fake_st(monkeypatch, _ExplodingST)

    events, sink = _record()
    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(config={}, event_sink=sink)
    with pytest.raises(RuntimeError, match="simulated load failure"):
        _run(p.embed(
            ["x"],
            "sentence-transformers/all-MiniLM-L6-v2",
        ))
    kinds = [k for k, _t, _m in events]
    assert kinds == ["status", "error"], (
        f"expected [status, error]; got {kinds}"
    )
    assert "reyn embeddings clear" in events[1][2]["retry_hint"]


# ── 5. Sink exception is swallowed ───────────────────────────────────────────


def test_sink_exception_does_not_crash_load(monkeypatch, tmp_path) -> None:
    """Tier 2: a buggy sink callable must not break the embedding load."""
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    def _bad_sink(kind: str, text: str, meta: dict) -> None:
        raise RuntimeError("sink imploded")

    from reyn.embedding.sentence_transformers_provider import (
        SentenceTransformersEmbeddingProvider,
    )
    p = SentenceTransformersEmbeddingProvider(
        config={}, event_sink=_bad_sink,
    )
    # Should NOT raise — the sink's exception is swallowed.
    result = _run(p.embed(
        ["x"],
        "sentence-transformers/all-MiniLM-L6-v2",
    ))
    assert result["vectors"]


# ── 6. Routing wrapper forwards the sink ─────────────────────────────────────


def test_routing_wrapper_forwards_event_sink_to_st_backend(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: a sink passed to RoutingEmbeddingProvider reaches the ST backend.

    The wrapper constructs the ST backend lazily; the sink supplied at
    the wrapper layer must be forwarded to that lazy construction.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    events, sink = _record()
    from reyn.config import EmbeddingClassSpec, EmbeddingConfig
    from reyn.embedding.router_provider import RoutingEmbeddingProvider

    cfg = EmbeddingConfig(
        default_class="local-mini",
        classes={
            "local-mini": EmbeddingClassSpec(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
        },
        batch_size=100,
        max_concurrent_batches=1,
        max_retries=3,
        retry_backoff="exponential",
        tokenizer="cl100k_base",
    )
    provider = RoutingEmbeddingProvider(config=cfg, event_sink=sink)
    _run(provider.embed(["x"], "local-mini"))
    # Backend was lazily constructed and emitted lifecycle events.
    assert [k for k, _t, _m in events] == ["status", "skill_done"]


# ── 7. get_provider passes the sink through ──────────────────────────────────


def test_get_provider_threads_event_sink_through_routing_wrapper(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: ``get_provider("litellm", config, event_sink=…)`` plumbs the sink.

    The factory must hand the sink to the routing wrapper so production
    callers (= ChatSession) only need to call ``get_provider`` once.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    events, sink = _record()
    from reyn.config import EmbeddingClassSpec, EmbeddingConfig
    from reyn.embedding import get_provider

    cfg = EmbeddingConfig(
        default_class="local-mini",
        classes={
            "local-mini": EmbeddingClassSpec(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
        },
        batch_size=100,
        max_concurrent_batches=1,
        max_retries=3,
        retry_backoff="exponential",
        tokenizer="cl100k_base",
    )
    provider = get_provider("litellm", cfg, event_sink=sink)
    _run(provider.embed(["x"], "local-mini"))
    assert [k for k, _t, _m in events] == ["status", "skill_done"]


def test_get_provider_without_event_sink_keeps_backward_compat(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: ``get_provider`` without ``event_sink`` returns a working wrapper.

    Pre-C.3 callers (= those who don't pass the new kwarg) keep working
    — the wrapper is byte-identical from their POV.
    """
    monkeypatch.setenv("REYN_CACHE_DIR", str(tmp_path))
    _FakeST.instances.clear()
    _install_fake_st(monkeypatch, _FakeST)

    from reyn.config import EmbeddingClassSpec, EmbeddingConfig
    from reyn.embedding import get_provider

    cfg = EmbeddingConfig(
        default_class="local-mini",
        classes={
            "local-mini": EmbeddingClassSpec(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
        },
        batch_size=100,
        max_concurrent_batches=1,
        max_retries=3,
        retry_backoff="exponential",
        tokenizer="cl100k_base",
    )
    provider = get_provider("litellm", cfg)  # no event_sink kwarg
    result = _run(provider.embed(["x"], "local-mini"))
    assert result["vectors"]
