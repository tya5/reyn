"""reyn.config.embedding — embedding + retrieval config: Embedding/ActionRetrieval. (#1682 #3 split)."""
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
#: All classes route through litellm — reyn depends on litellm exclusively
#: for embeddings, no in-process model backend (#3128 removed the
#: in-process local-model-backed builtin classes that shipped under
#: FP-0043; operators who want a local model reach it via a
#: litellm-fronted proxy and an operator-defined ``embedding.classes`` entry
#: instead).
_DEFAULT_EMBEDDING_CLASSES: dict[str, EmbeddingClassSpec] = {
    "light":     EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "standard":  EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "strong":    EmbeddingClassSpec(model="openai/text-embedding-3-large"),
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
        timeout:       Per-attempt deadline in seconds — how long reyn WAITS for
                       one embedding attempt (#3043). ``<= 0`` opts out (= no
                       bound), mirroring the MCP gateway's
                       ``call_timeout_seconds`` contract.
                       Bounds waiting, NOT spending: the OpenAI SDK client
                       retries beneath this knob, so one attempt can deliver up
                       to 3 requests and ``max_retries: 3`` up to 9 (measured:
                       all 9 delivered in 7.6s under the 60.0s default, which
                       never engages). Lowering it does not lower that count —
                       reducing REQUESTS is a separate lever, open in #3047.
                       Default 60.0 == ``chat.timeout.llm_call_seconds``: an
                       embedding call is the same KIND of thing as a chat LLM
                       call (one HTTP round-trip to a model provider), so it
                       carries the same bound — unlike an MCP call (120.0),
                       which also pays a subprocess spawn. Without this the
                       effective bound was litellm's own ``request_timeout``
                       default of 6000s (= 100 min/attempt, ~5h across
                       ``max_retries``) — indistinguishable from a hang.
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
    timeout: float = 60.0
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
    raw_timeout = raw.get("timeout", defaults.timeout)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        raise ValueError(
            f"embedding.timeout must be a number of seconds, got {raw_timeout!r}"
        ) from None
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
    if timeout <= 0:
        logging.getLogger(__name__).warning(
            "embedding.timeout=%s opts OUT of the per-attempt bound: an embedding "
            "API call that stalls will run to litellm's own request_timeout "
            "(6000s/attempt) with nothing to interrupt it (#3043). Set a positive "
            "number of seconds to restore a finite bound.",
            timeout,
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
        timeout=timeout,
        tokenizer=tokenizer,
        cost_warn_threshold=cost_warn_threshold,
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

            **Default since the semantic-search-opt-in fix (2026)**:
            ``None`` (= off). ``search_actions`` is a semantic-search
            feature and the project's standing principle is that
            semantic search is opt-in, never on by default — a
            default of a built-in local-model class (FP-0043 Phase 4
            through 2026) made reyn attempt a Hugging Face model download at
            startup even for zero-config / offline installs, which
            surfaced as a startup warning when the download failed.
            Defaulting to ``None`` makes the off-path silent: no
            index build is attempted, so there is nothing to fail or
            warn about; ``search_actions`` is simply absent from
            tools= (§D14 gating).

            Operators who want semantic ``search_actions`` set
            ``action_retrieval.embedding_class`` explicitly in
            reyn.yaml: ``standard`` (= or ``light`` / ``strong``) for
            the built-in OpenAI-backed classes, or a custom entry
            under ``embedding.classes`` pointing at any litellm-
            routable model (including a local model served behind a
            litellm-fronted proxy — #3128 removed the in-process
            local-model embedding backend; reyn depends on litellm
            exclusively for embeddings).

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
    embedding_class: str | None = None
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
