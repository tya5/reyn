"""SentenceTransformersEmbeddingProvider — local HF model embedding backend.

FP-0043 Phase 2 Component B. Implements the ``EmbeddingProvider`` protocol
against a locally-loaded sentence-transformers model (= no network call,
no API credentials).

Activation
----------
Reached only when the configured ``EmbeddingClassSpec.model`` carries the
``sentence-transformers/`` prefix; the routing layer in
``reyn.embedding.RoutingEmbeddingProvider`` dispatches to this backend.
For consumers calling the protocol directly (= ``get_provider("st", ...)``),
this class can also be instantiated standalone.

Lazy loading
------------
The ``sentence_transformers`` import + model download + weights load
happen on the FIRST ``embed()`` call, NOT in ``__init__``. This keeps
``reyn chat`` boot fast for users who never trigger semantic search:
the cost (= ~22MB download + ~1-2s torch import) is paid lazily by the
first ``search_actions`` call. Subsequent calls reuse the cached model
in-process.

Cache path precedence (= O2 reconcile)
--------------------------------------
1. ``REYN_CACHE_DIR`` — explicit reyn-specific override (wins)
2. ``XDG_CACHE_HOME`` — linux convention backstop
3. ``~/.cache/reyn/`` — final default

The HF model cache lives under ``<resolved>/sentence-transformers/``;
this matches the ``sentence_transformers`` library's existing
``cache_folder`` convention while keeping reyn artifacts under a single
top-level cache root.

Device selection (= REYN_EMBED_DEVICE env opt-in)
-------------------------------------------------
- Default: ``cpu`` (= explicit, predictable, matches the FP-0043
  Non-goals stance "no GPU auto-detection")
- Override: set ``REYN_EMBED_DEVICE`` to one of ``cpu`` / ``mps`` /
  ``cuda`` to opt into GPU acceleration when available. Invalid values
  fall back to cpu with a warning.

Failure modes
-------------
- ``sentence_transformers`` import fails (= extras not installed):
  ``embed()`` raises ``ImportError`` with the canonical install command.
  Callers should catch this and fall back to ``search_actions`` hidden
  per the §D14 visibility gate.
- Model download fails (= no network, registry error): propagates the
  underlying exception; the index build path swallows it and leaves
  the index unbuilt (= ``is_ready()`` returns False, search_actions
  stays hidden).
"""
from __future__ import annotations

import asyncio
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.embedding.provider import EmbedBatchResult

# FP-0043 Component C.3 — first-time DL progress UX.
#
# An EventSink is an optional ``(kind, text, meta) -> None`` callable
# the caller passes in to receive lifecycle notifications during the
# lazy model load. The provider emits:
#
#   ("status",     "<msg>", {model, target_dir, device})   on DL start
#   ("skill_done", "<msg>", {model, dimension})            on load complete
#   ("error",      "<msg>", {model, retry_hint})           on load failure
#
# The sink is called synchronously from inside ``_load`` (= which itself
# runs under ``asyncio.to_thread`` when invoked via ``embed``). Sink
# implementations that need to bridge into an async event bus should
# either be thread-safe or use a thread-safe primitive (= queue,
# loop.call_soon_threadsafe). Sink exceptions are swallowed so a
# misbehaving sink can never break the embedding load.
EventSink = Callable[[str, str, dict], None]

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer  # noqa: F401


_PREFIX = "sentence-transformers/"
_VALID_DEVICES = {"cpu", "mps", "cuda"}
_INSTALL_HINT = (
    "sentence_transformers is not installed. "
    "Run: pip install 'reyn[local-embed]'"
)


def is_available() -> bool:
    """Whether the optional sentence-transformers extras are installed.

    Cheap probe using importlib.util.find_spec — does NOT import the
    heavy module, just checks whether the import would succeed. Used by
    callers that need to decide between an ST-backed path and a graceful
    fallback before any actual embed() call (= FP-0043 Phase 4 default-
    switch graceful-degrade in :class:`ChatSession`).
    """
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def _resolve_cache_dir() -> Path:
    """Resolve the cache root per O2 reconcile precedence."""
    if v := os.environ.get("REYN_CACHE_DIR"):
        root = Path(v).expanduser()
    elif v := os.environ.get("XDG_CACHE_HOME"):
        root = Path(v).expanduser() / "reyn"
    else:
        root = Path.home() / ".cache" / "reyn"
    return root / "sentence-transformers"


def _resolve_device() -> str:
    """Resolve the inference device from ``REYN_EMBED_DEVICE``.

    Invalid values warn and fall back to ``cpu`` rather than failing
    hard — this matches the FP-0043 "GPU is opt-in / out-of-goal but
    don't deny escape hatch" stance.
    """
    raw = os.environ.get("REYN_EMBED_DEVICE", "cpu").lower().strip()
    if raw in _VALID_DEVICES:
        return raw
    warnings.warn(
        f"REYN_EMBED_DEVICE={raw!r} is not one of {sorted(_VALID_DEVICES)}; "
        f"falling back to 'cpu'",
        stacklevel=2,
    )
    return "cpu"


def _strip_prefix(model: str) -> str:
    """Drop the ``sentence-transformers/`` prefix to leave the HF model id."""
    if model.startswith(_PREFIX):
        return model[len(_PREFIX):]
    return model


class SentenceTransformersEmbeddingProvider:
    """EmbeddingProvider backed by sentence-transformers local models.

    Args:
        config: Either an ``EmbeddingConfig`` dataclass (production path)
            or a plain dict (test / legacy). Used only for the model-class
            lookup table; this provider does NOT honour batch_size /
            max_concurrent_batches at the moment — sentence-transformers
            encodes in a single call per batch internally.
        event_sink: Optional ``(kind, text, meta) -> None`` callable
            that receives lifecycle notifications during the lazy model
            load (= "downloading...", "loaded", "error"). When ``None``
            the load runs silently — preserves the pre-#922 behaviour.
            FP-0043 Component C.3 onboarding UX.
    """

    def __init__(
        self,
        config: "dict[str, Any] | Any | None" = None,
        *,
        event_sink: "EventSink | None" = None,
    ) -> None:
        if config is None:
            config = {}
        if not isinstance(config, dict) and hasattr(config, "classes"):
            self._classes = {
                name: spec.model for name, spec in config.classes.items()
            }
            self.tokenizer = str(getattr(config, "tokenizer", "cl100k_base"))
        else:
            self._classes = config.get("classes", {})
            self.tokenizer = config.get("tokenizer", "cl100k_base")
        self._models: dict[str, Any] = {}
        self._cache_dir = _resolve_cache_dir()
        self._device = _resolve_device()
        self._event_sink: "EventSink | None" = event_sink

    # ── Event sink helper ─────────────────────────────────────────────────

    def _emit(self, kind: str, text: str, meta: "dict | None" = None) -> None:
        """Dispatch a lifecycle event through the configured sink, if any.

        Sink exceptions are intentionally swallowed: an event-bus
        misconfiguration must not prevent the embedding load from
        completing (= the LLM still needs vectors regardless of
        whether the operator's TUI got a "downloading..." line).
        """
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(kind, text, meta or {})
        except Exception:
            pass

    # ── Model resolution ───────────────────────────────────────────────────

    def resolve_model(self, model: str) -> str:
        """Resolve a class name to the underlying ``sentence-transformers/<id>``.

        If ``model`` already carries the prefix or a ``/``, return as-is.
        Otherwise look it up in the configured classes map.
        """
        if "/" in model:
            return model
        if model in self._classes:
            spec = self._classes[model]
            return spec.model if hasattr(spec, "model") else str(spec)
        return model

    # ── Lazy load ──────────────────────────────────────────────────────────

    def _load(self, resolved_model: str) -> Any:
        """Load (and cache in-process) the sentence-transformers model.

        Raises ImportError when the optional dep is missing — the caller
        is expected to surface this with the canonical install hint
        (already embedded in the exception message).

        Lifecycle events (FP-0043 Component C.3):
          * ``status`` before the load begins, carrying ``model`` /
            ``target_dir`` / ``device`` so the TUI can render a sticky
            status row.
          * ``skill_done`` after the load succeeds, with the resolved
            dimension for cross-checking.
          * ``error`` if the load (or the underlying import) fails,
            with a ``retry_hint`` pointing at the relevant recovery
            command.
        """
        cached = self._models.get(resolved_model)
        if cached is not None:
            return cached
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            self._emit(
                "error",
                f"sentence_transformers not installed; cannot load "
                f"{resolved_model!r}",
                {
                    "model": resolved_model,
                    "retry_hint": (
                        "Run `pip install 'reyn[local-embed]'` to "
                        "install the local embedding extras, then retry."
                    ),
                },
            )
            raise ImportError(_INSTALL_HINT) from exc

        hf_id = _strip_prefix(resolved_model)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._emit(
            "status",
            f"loading embedding model {resolved_model!r} "
            f"(first run may download ~20-120 MB)",
            {
                "model": resolved_model,
                "target_dir": str(self._cache_dir),
                "device": self._device,
            },
        )
        try:
            model = SentenceTransformer(
                hf_id,
                cache_folder=str(self._cache_dir),
                device=self._device,
            )
        except Exception as exc:
            self._emit(
                "error",
                f"failed to load {resolved_model!r}: {exc}",
                {
                    "model": resolved_model,
                    "retry_hint": (
                        "Check network connectivity, then retry. If the "
                        "cache is partial / corrupt, run "
                        "`reyn embeddings clear` to wipe and start fresh."
                    ),
                },
            )
            raise
        # Resolve dimension without re-loading; sentence-transformers
        # exposes this on the loaded instance.
        try:
            dim = int(model.get_sentence_embedding_dimension())
        except Exception:
            dim = 0
        self._emit(
            "skill_done",
            f"loaded embedding model {resolved_model!r} ({dim}d)",
            {"model": resolved_model, "dimension": dim},
        )
        self._models[resolved_model] = model
        return model

    # ── Token estimation ───────────────────────────────────────────────────

    def estimate_tokens(self, texts: list[str]) -> int:
        """Rough token count for cost preflight; no API call.

        For local models there's no per-token charge, but the
        EmbeddingProvider protocol still requires this method (used by
        upstream cost-preflight gates). We fall back to the same
        char-based heuristic the litellm provider uses on tiktoken
        failure: ``len(text) // 4`` per text.
        """
        return sum(len(t) // 4 for t in texts)

    # ── Dimension ──────────────────────────────────────────────────────────

    def get_dimension(self, model: str) -> int:
        """Return the model's embedding dimension.

        Lazy-loads the model to ask it directly (= no static dimension
        table for local models; sentence-transformers exposes this on
        the loaded instance).
        """
        resolved = self.resolve_model(model)
        m = self._load(resolved)
        return int(m.get_sentence_embedding_dimension())

    # ── Core embed ─────────────────────────────────────────────────────────

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        """Encode ``texts`` via the local sentence-transformers model.

        The encode call is CPU-bound; we run it in the default thread
        pool via ``run_in_executor`` to avoid blocking the event loop.

        Returns:
            EmbedBatchResult with vectors in input order. ``total_tokens``
            is the estimate (no API per-token billing for local models).
        """
        resolved = self.resolve_model(model)
        if not texts:
            return EmbedBatchResult(vectors=[], model=resolved, total_tokens=0)

        m = self._load(resolved)
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(
            None,
            lambda: m.encode(
                texts,
                convert_to_numpy=False,  # return list[Tensor] → coerce below
                show_progress_bar=False,
                normalize_embeddings=False,
            ),
        )
        # Coerce torch.Tensor → list[float] for protocol shape.
        out_vectors: list[list[float]] = []
        for v in vectors:
            if hasattr(v, "tolist"):
                out_vectors.append([float(x) for x in v.tolist()])
            else:
                out_vectors.append([float(x) for x in v])

        return EmbedBatchResult(
            vectors=out_vectors,
            model=resolved,
            total_tokens=self.estimate_tokens(texts),
        )


__all__ = [
    "SentenceTransformersEmbeddingProvider",
    "EventSink",
    "_PREFIX",  # exported for the routing layer
    "_resolve_cache_dir",  # exported for tests
    "_resolve_device",
]
