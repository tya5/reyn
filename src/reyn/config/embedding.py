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
        enabled:       FP-0066 §7 — single opt-in switch for the whole
                       embedding-backed semantic-discovery layer (action
                       retrieval `search_actions` + knowledge retrieval +
                       the FP-0063 plugin's `rag_ingest` embed step).
                       Default **False** (opt-in / predictable-safe
                       default — embedding needs a provider + cost).
                       Clean-break replacement for the fragmented
                       ``action_retrieval.embedding_class`` on/off gate
                       (#3218 / FP-0066 §7): the on/off decision now lives
                       HERE, single switch, no per-group/source
                       granularity (YAGNI). The MODEL used when enabled is
                       ``default_class`` below (unchanged field, already
                       defaulted to ``"standard"``) — ``enabled`` and
                       ``default_class`` are orthogonal (which model
                       vs whether to embed at all).

                       Symmetric model (§7): ``enabled=False`` hides only
                       the *semantic-discovery* layer (`search_actions` /
                       future `search_knowledge`) — `list_*` discovery and
                       load/read/invoke verbs are unaffected.
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

    enabled: bool = False
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

    defaults_for_enabled = EmbeddingConfig()
    raw_enabled = raw.get("enabled", defaults_for_enabled.enabled)
    if not isinstance(raw_enabled, bool):
        raise ValueError(
            f"embedding.enabled must be a bool, got {type(raw_enabled).__name__}"
        )
    enabled = raw_enabled

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
        enabled=enabled,
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

    #4552: the arc that emptied this class.

    - PR-1: ``hot_list_n``/``hot_list_seed`` discarded — owner directive,
      hot list's role is gone, superseded by ``list_actions`` as the
      canonical discovery path.
    - PR-2: ``mode`` (§D24 operational-mode label) removed — 0 real
      consumers, confirmed via census in the PR body.
    - PR-3 (this one): ``universal_wrappers_enabled`` MOVED to
      ``tool_use.universal_wrappers_enabled`` (see
      ``execution.ToolUseConfig``) — architect's ruling: it is a
      ``tool_use`` (presentation-scheme) property, not a retrieval
      setting; only ``universal-category``'s own 3 wrapper functions ever
      read it. Unblocked once #4564 removed the flag's one remaining
      undeclared reach into ``search_actions`` visibility (now solely
      ``embedding.enabled``'s).

    This dataclass is now EMPTY — PR-4 deletes ``action_retrieval:``
    (and this class) entirely, closing the #4552 arc.
    """


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

    # #4552 PR-3: the last recognised field (universal_wrappers_enabled)
    # moved to tool_use.universal_wrappers_enabled — every key reaching
    # here is now unknown (forward-compatible: ignored, not an error).
    # PR-4 deletes this function and the section entirely.
    return ActionRetrievalConfig()
