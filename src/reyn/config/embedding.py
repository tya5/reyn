"""reyn.config.embedding — embedding + retrieval config: Embedding/SkillSearch/ActionRetrieval. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


@dataclass
class EmbeddingClassSpec:
    """A single class entry under ``embedding.classes``.

    Mirrors ModelSpec for embedding endpoints. Supports str
    (``'openai/text-embedding-3-small'``) or dict (``{model: '...',
    api_base: '${VAR}', extra_body: {...}}``) form in YAML.
    ``extends`` is resolved at parse time and not stored here.

    ADR-0033 Phase 1 — ``reyn.yaml`` ``embedding:`` section.
    """

    model: str                                      # canonical "<provider>/<name>"
    api_base: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


#: Built-in defaults for ``embedding.classes``.
#: Applied when the section is absent or ``classes:`` is empty.
#: Satisfies the "pip install + OPENAI_API_KEY = works" requirement.
#:
#: FP-0043: ``local-mini`` and ``local-e5`` ship in the registry but only
#: function when ``pip install 'reyn[local-embed]'`` (= sentence-transformers
#: + torch extras) has been performed. Without the extras, instantiating
#: those classes raises ImportError at first ``embed()`` call; the
#: ``search_actions`` visibility gate falls back to "hidden" gracefully.
_DEFAULT_EMBEDDING_CLASSES: dict[str, EmbeddingClassSpec] = {
    "light":     EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "standard":  EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "strong":    EmbeddingClassSpec(model="openai/text-embedding-3-large"),
    "local-mini": EmbeddingClassSpec(
        model="sentence-transformers/all-MiniLM-L6-v2",
    ),
    "local-e5":  EmbeddingClassSpec(
        model="sentence-transformers/intfloat/multilingual-e5-small",
    ),
}


@dataclass
class EmbeddingConfig:
    """`embedding:` — RAG embedding settings (ADR-0033 Phase 1).

    Built-in defaults cover the common OpenAI path so users can start
    indexing after ``pip install reyn`` + ``OPENAI_API_KEY`` with no
    ``reyn.yaml`` changes required.

    Fields:
        default_class: Name of the class used when callers don't specify one.
        classes:       Named embedding class → EmbeddingClassSpec mapping.
        batch_size:    Texts per embedding API call (1–2048).
        max_concurrent_batches:
                       Parallel batch calls in flight (1–10).
                       Phase 1 forces 1; values > 1 are accepted but
                       logged as warnings until the concurrent path lands.
        max_retries:   Transient-error retries (0–10).
        retry_backoff: Backoff strategy: ``'exponential'`` or ``'linear'``.
        tokenizer:     tiktoken encoding used for chunk-size estimation.
        cost_warn_threshold:
                       Ask-user gate fires when estimated chunk count
                       exceeds this value (UX gap fix B, ADR-0033 §2.1).
    """

    default_class: str = "standard"
    classes: dict[str, EmbeddingClassSpec] = field(
        default_factory=lambda: dict(_DEFAULT_EMBEDDING_CLASSES)
    )
    batch_size: int = 100
    max_concurrent_batches: int = 1
    max_retries: int = 3
    retry_backoff: Literal["exponential", "linear"] = "exponential"
    tokenizer: str = "cl100k_base"
    cost_warn_threshold: int = 10000

    def resolve_class(self, name: str) -> EmbeddingClassSpec:
        """Look up a class by name; raise ``KeyError`` if unknown."""
        return self.classes[name]


def _parse_embedding_classes(raw: dict[str, Any]) -> dict[str, EmbeddingClassSpec]:
    """Parse the ``embedding.classes`` dict.

    Each entry may be a str (shorthand model name) or a dict with at
    least a ``model`` key. Dict entries support a shallow ``extends``
    lookup within the same raw classes dict (one level only — cycles
    are not checked; multi-level chains are a phase-2 concern).

    Raises:
        ValueError: unknown extends target, missing ``model``, or
                    entry value that is neither str nor dict.
    """
    result: dict[str, EmbeddingClassSpec] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            result[name] = EmbeddingClassSpec(model=value)
        elif isinstance(value, dict):
            if "extends" in value:
                base_name = value["extends"]
                base = raw.get(base_name)
                if isinstance(base, str):
                    base_dict: dict[str, Any] = {"model": base}
                elif isinstance(base, dict):
                    base_dict = {k: v for k, v in base.items() if k != "extends"}
                else:
                    raise ValueError(
                        f"embedding.classes.{name} extends '{base_name}' "
                        f"which doesn't exist in embedding.classes"
                    )
                # Override: base fields replaced by explicit values (extends stripped).
                merged: dict[str, Any] = {
                    **base_dict,
                    **{k: v for k, v in value.items() if k != "extends"},
                }
            else:
                merged = dict(value)
            if "model" not in merged:
                raise ValueError(
                    f"embedding.classes.{name} is missing the required 'model' field"
                )
            result[name] = EmbeddingClassSpec(
                model=str(merged["model"]),
                api_base=(str(merged["api_base"]) if merged.get("api_base") is not None else None),
                extra_body=dict(merged.get("extra_body") or {}),
            )
        else:
            raise ValueError(
                f"embedding.classes.{name} must be a str or dict, "
                f"got {type(value).__name__}"
            )
    # #1454 PR-B: name-position validation. A ``model`` value is a NAME
    # position, which should be ``provider/model`` (the `/`-prefix invariant —
    # all builtin defaults comply). WARN (not error) for a bare name: litellm
    # may accept some bare strings, so bare usage is degraded-but-allowed,
    # flagged so a misroute is diagnosable. (Class positions are closed-world;
    # name positions allow the prefixed literal — the unified class/name rule.)
    for _name, _spec in result.items():
        if "/" not in _spec.model:
            import logging

            logging.getLogger(__name__).warning(
                "embedding.classes.%s model %r has no provider prefix ('/') — "
                "a model position should be 'provider/model' (e.g. "
                "'openai/text-embedding-3-small'). Treating as a bare LiteLLM "
                "name; add the prefix if embedding resolution misroutes.",
                _name, _spec.model,
            )
    return result


def _build_embedding_config(raw: object) -> EmbeddingConfig:
    """Parse the ``embedding:`` section. Empty / missing returns full defaults.

    Validation rules (raise ``ValueError`` on violation):
      - batch_size: 1–2048
      - max_concurrent_batches: 1–10
      - max_retries: 0–10
      - retry_backoff: ``'exponential'`` or ``'linear'``
      - default_class must be a key in the resolved classes dict

    ``${VAR}`` interpolation is already applied to *raw* by the top-level
    loader (ADR-0030) — no special handling is needed here.
    """
    import logging

    if not isinstance(raw, dict):
        return EmbeddingConfig(classes=dict(_DEFAULT_EMBEDDING_CLASSES))

    raw_classes = raw.get("classes") or {}
    if not isinstance(raw_classes, dict):
        raw_classes = {}

    classes = _parse_embedding_classes(raw_classes) if raw_classes else dict(_DEFAULT_EMBEDDING_CLASSES)

    defaults = EmbeddingConfig()
    batch_size = int(raw.get("batch_size", defaults.batch_size))
    max_concurrent_batches = int(raw.get("max_concurrent_batches", defaults.max_concurrent_batches))
    max_retries = int(raw.get("max_retries", defaults.max_retries))
    retry_backoff = str(raw.get("retry_backoff", defaults.retry_backoff))
    tokenizer = str(raw.get("tokenizer", defaults.tokenizer))
    cost_warn_threshold = int(raw.get("cost_warn_threshold", defaults.cost_warn_threshold))
    default_class = str(raw.get("default_class", defaults.default_class))

    if not (1 <= batch_size <= 2048):
        raise ValueError(
            f"embedding.batch_size must be 1–2048, got {batch_size}"
        )
    if not (1 <= max_concurrent_batches <= 10):
        raise ValueError(
            f"embedding.max_concurrent_batches must be 1–10, got {max_concurrent_batches}"
        )
    if max_concurrent_batches > 1:
        logging.getLogger(__name__).warning(
            "embedding.max_concurrent_batches=%d is set but concurrent batch "
            "support is not yet active in phase 1; value is accepted and will "
            "take effect when the concurrent path lands.",
            max_concurrent_batches,
        )
    if not (0 <= max_retries <= 10):
        raise ValueError(
            f"embedding.max_retries must be 0–10, got {max_retries}"
        )
    if retry_backoff not in {"exponential", "linear"}:
        raise ValueError(
            f"embedding.retry_backoff must be 'exponential' or 'linear', "
            f"got {retry_backoff!r}"
        )
    if default_class not in classes:
        raise ValueError(
            f"embedding.default_class '{default_class}' is not a key in "
            f"embedding.classes; available: {sorted(classes)}"
        )

    return EmbeddingConfig(
        default_class=default_class,
        classes=classes,
        batch_size=batch_size,
        max_concurrent_batches=max_concurrent_batches,
        max_retries=max_retries,
        retry_backoff=retry_backoff,  # type: ignore[arg-type]
        tokenizer=tokenizer,
        cost_warn_threshold=cost_warn_threshold,
    )


@dataclass
class SkillSearchConfig:
    """`skill_search:` — BM25 skill pre-filter settings (FP-0024 Component A).

    When the catalogue exceeds ``threshold`` skills, the router narrows
    ``invoke_skill.name`` enum to the top-``top_k`` BM25 keyword matches
    before building the tools list.  Falls through to full enum on 0 BM25
    results (= no skill made invisible).

    Fields:
        threshold:  Catalogue size at which BM25 activates. Default 20.
                    Set 0 to always pre-filter; set a high number to disable.
        top_k:      Number of skills returned by BM25. Default 5.
        backend:    ``'bm25'`` (default). ``'embedding'`` / ``'hybrid'``
                    reserved for Component C/D.
    """

    threshold: int = 20
    top_k: int = 5
    backend: str = "bm25"


def _build_skill_search_config(raw: object) -> "SkillSearchConfig":
    """Parse the ``skill_search:`` section. Empty / missing returns defaults."""
    defaults = SkillSearchConfig()
    if not isinstance(raw, dict):
        return defaults
    threshold_raw = raw.get("threshold", defaults.threshold)
    top_k_raw = raw.get("top_k", defaults.top_k)
    backend_raw = raw.get("backend", defaults.backend)
    try:
        threshold = int(threshold_raw)
        if threshold < 0:
            threshold = 0
    except (TypeError, ValueError):
        threshold = defaults.threshold
    try:
        top_k = int(top_k_raw)
        if top_k < 1:
            top_k = 1
    except (TypeError, ValueError):
        top_k = defaults.top_k
    return SkillSearchConfig(
        threshold=threshold,
        top_k=int(top_k),
        backend=str(backend_raw),
    )


@dataclass
class ActionRetrievalConfig:
    """`action_retrieval:` — FP-0034 universal catalog + retrieval settings.

    Phase 1 of FP-0034. The 4 universal wrappers (list_actions /
    search_actions / describe_action / invoke_action) plus the
    qualified-name dispatcher land across PR-1 through PR-3b-iv.
    Subsequent phases extend with hot list / cold start /
    search_actions enablement.

    Fields:
        universal_wrappers_enabled:
            When True (= **default since PR-3b-iv**), ``build_tools()``
            appends the 3 universal wrappers (list_actions /
            describe_action / invoke_action) at the end of tools=.
            ``search_actions`` is gated separately via
            ``embedding_class`` per §D14.

            The flip from False (= PR-3b-i through iii) to True
            happens here in PR-3b-iv. Operators who want to opt out
            (= preserve the prior tools= shape) can set
            ``action_retrieval.universal_wrappers_enabled: false``
            in reyn.yaml.

            Test suite verified safe via FakeRouterHost insulation:
            all LLMReplay fixtures + AsyncMock-based E2E tests do
            NOT implement ``get_universal_wrappers_enabled`` so the
            RouterLoop's getattr fallback keeps tools= shape stable
            for the recorded fixtures. The flip affects production
            runtime only.

        embedding_class:
            Name of the entry in ``embedding.classes`` to use for
            action retrieval semantic search (= §D13). When None or
            empty, ``search_actions`` is excluded from tools= even if
            ``universal_wrappers_enabled`` is True (§D14 gating).

            **Default since FP-0043 Phase 4**: ``"local-mini"`` (=
            ``sentence-transformers/all-MiniLM-L6-v2``). When the
            ``local-embed`` extras are not installed (= ``import
            sentence_transformers`` fails), ChatSession silently
            degrades to None — ``search_actions`` stays hidden and
            ``list_actions`` injects a hidden-state hint pointing
            operators at ``pip install 'reyn[local-embed]'``. So the
            new default is "active when the import succeeds, no-op
            otherwise", giving zero-config fresh users semantic
            search the moment they install the extras.

            Operators who want OpenAI-backed embeddings instead can
            set ``action_retrieval.embedding_class: standard`` (= or
            ``light`` / ``strong``) explicitly in reyn.yaml. Setting
            it to ``null`` or empty disables ``search_actions``
            entirely.

        hot_list_n:
            Hot list size for top-N freq+recency projection (§D2).
            Default 0 (= off) following N=0 viability verdict (44 runs,
            nested-args 0/23) — list_actions is the canonical discovery
            path and hot-list aliases introduced a visibility-asymmetry
            bug class. Operators who want aliases can set hot_list_n: 10
            (or higher) in reyn.yaml; the seed, tracker, and alias-builder
            mechanisms remain fully operative as an opt-in.

        mode:
            Operational mode label (§D24): ``"minimal"`` /
            ``"default"`` / ``"performance"``. Stored as a free-form
            string so callers can layer interpretations on top
            without further config breaking changes. Default
            ``"default"`` is the §D24 balanced setting.
    """

    universal_wrappers_enabled: bool = True
    embedding_class: str | None = "local-mini"
    hot_list_n: int = 0
    mode: str = "default"
    # FP-0034 §D16: seed qualified names for initial hot list (before freq
    # accumulates). "default" means the OS-defined 10-item seed (5 universal
    # + 5 Reyn flagship). [] means no seed. Explicit list overrides the
    # default. Parsed by _build_action_retrieval_config.
    hot_list_seed: list[str] | str = "default"


def _build_action_retrieval_config(raw: object) -> ActionRetrievalConfig:
    """Parse ``action_retrieval:`` from reyn.yaml.

    Accepts a dict with any subset of fields; unknown keys are
    ignored (= forward-compatible with future Phase 2 additions).
    Validates types and clamps numeric ranges to non-negative.

    Raises:
        ValueError: when a recognised field has an invalid type
            (= explicit type mismatch; missing fields fall back to
            defaults).
    """
    if raw is None:
        return ActionRetrievalConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"action_retrieval must be a mapping, got {type(raw).__name__}"
        )

    cfg = ActionRetrievalConfig()

    if "universal_wrappers_enabled" in raw:
        val = raw["universal_wrappers_enabled"]
        if not isinstance(val, bool):
            raise ValueError(
                "action_retrieval.universal_wrappers_enabled must be a bool, "
                f"got {type(val).__name__}"
            )
        cfg.universal_wrappers_enabled = val

    if "embedding_class" in raw:
        val = raw["embedding_class"]
        if val is not None and not isinstance(val, str):
            raise ValueError(
                "action_retrieval.embedding_class must be a string or null, "
                f"got {type(val).__name__}"
            )
        cfg.embedding_class = val or None

    if "hot_list_n" in raw:
        val = raw["hot_list_n"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise ValueError(
                "action_retrieval.hot_list_n must be an integer, "
                f"got {type(val).__name__}"
            )
        if val < 0:
            raise ValueError(
                f"action_retrieval.hot_list_n must be >= 0, got {val}"
            )
        cfg.hot_list_n = val

    if "mode" in raw:
        val = raw["mode"]
        if not isinstance(val, str):
            raise ValueError(
                f"action_retrieval.mode must be a string, got {type(val).__name__}"
            )
        cfg.mode = val

    if "hot_list_seed" in raw:
        val = raw["hot_list_seed"]
        if val == "default":
            cfg.hot_list_seed = "default"
        elif isinstance(val, list):
            for item in val:
                if not isinstance(item, str):
                    raise ValueError(
                        "action_retrieval.hot_list_seed list items must be "
                        f"strings, got {type(item).__name__}"
                    )
            cfg.hot_list_seed = list(val)
        else:
            raise ValueError(
                "action_retrieval.hot_list_seed must be \"default\" or a "
                f"list of strings, got {type(val).__name__!r}"
            )

    return cfg
