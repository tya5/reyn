"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong.
Mapping is provided by ReynConfig.models (loaded from reyn.yaml).
Unknown names pass through unchanged (backward compatible with raw LiteLLM strings).

PR-MODEL-SPEC-EXTENDS: adds ``extends`` field + built-in catalog support.

Disambiguation heuristic for str-form values:
  - value contains ``/``  -> literal LiteLLM model string (backward compat)
  - value has no ``/``    -> class reference shorthand (resolved via namespace)

Built-in catalog (``BUILTIN_MODELS``) is pre-loaded into the internal namespace.
User-declared entries override built-ins with the same name.

Startup fail-fast: all entries are resolved at ``__init__`` time.  Unresolvable
references, cycles, and missing ``model`` fields raise ``ValueError``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reyn.llm.builtin_models import BUILTIN_MODELS

#: The three standard model tiers. Users should map these in reyn.yaml.
STANDARD_CLASSES = ("light", "standard", "strong")

#: #1650: valid values for the per-model ``reasoning_effort`` field. These are
#: litellm's accepted reasoning-effort levels for the gemini provider, each of
#: which maps to a native thinking budget (low→1024, medium→2048, high→4096,
#: minimal→model-specific, disable/none→0; verified live against litellm 1.84.0
#: gemini/gemini-2.5-flash-lite). Validated at config-load (fail-fast) so a typo
#: surfaces at startup instead of raising mid-call inside litellm.
VALID_REASONING_EFFORTS: frozenset = frozenset(
    {"minimal", "low", "medium", "high", "disable", "none"}
)


@dataclass(frozen=True)
class ModelSpec:
    """Resolved model configuration for a single model class.

    ``model`` is the LiteLLM model string (required).
    ``kwargs`` carries any additional litellm kwargs declared by the operator
    in reyn.yaml (e.g. temperature, max_tokens, extra_body).  These are
    silently passed through to litellm — unknown fields are not validated
    (passthrough policy, Q1 Option B).
    """

    model: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    # #309 per-class routing (multi-provider). Explicit routing fields (NOT in
    # ``kwargs``) so a class can target its own endpoint/provider while others
    # use the global default — e.g. router=light on a Gemini proxy + skill=
    # capable on Anthropic-direct, simultaneously. Mirrors EmbeddingClassSpec
    # (which carries ``api_base``); ``provider`` is added for the direct-vs-proxy
    # opt-out. NO per-class api_key field by design: a literal secret must never
    # live in checked-in config — litellm resolves the key from the standard
    # provider env (OPENAI_API_KEY for a proxy, ANTHROPIC_API_KEY/GEMINI_API_KEY
    # for a direct provider). Both default None → inherit the global api_base.
    api_base: str | None = None
    provider: str | None = None  # litellm custom_llm_provider

    def __post_init__(self) -> None:
        # #1650: validate the operator-declared ``reasoning_effort`` at
        # config-load (fail-fast) rather than letting an invalid value reach
        # litellm and raise mid-call. The value itself rides the existing
        # ``kwargs`` passthrough to litellm.acompletion (call_llm:1155 /
        # call_llm_tools:1359), which maps it to the provider's native thinking
        # budget — we only gate the value set + the conflicting-config combo.
        # Placed here (not in from_config) so BOTH the plain-dict path and the
        # extends-merge path (which build ModelSpec directly) are covered
        # by-construction — single validation site, no parallel drift.
        effort = self.kwargs.get("reasoning_effort")
        if effort is None:
            return
        # #1654: two accepted forms —
        #   - str (gemini native): the effort level directly, e.g. "low".
        #   - dict (OpenAI/GPT-5 summary opt-in): {"effort": <level>, "summary":
        #     "detailed"}. OpenAI reasoning models don't return reasoning TEXT
        #     unless a summary is requested; litellm's GPT-5 transformation reads
        #     {effort, summary} (gpt_5_transformation.py). The level is validated
        #     either way; the optional "summary" rides through to litellm.
        if isinstance(effort, dict):
            level = effort.get("effort")
        elif isinstance(effort, str):
            level = effort
        else:
            level = None
        if level not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"models reasoning_effort must be one of "
                f"{sorted(VALID_REASONING_EFFORTS)} (a string), or a dict with "
                f"'effort' one of them (e.g. {{'effort': 'low', 'summary': "
                f"'detailed'}} for OpenAI summary text); got {effort!r} "
                f"(model={self.model!r})"
            )
        # Both-set reject: reasoning_effort already maps to a native thinking
        # budget, so a hand-set extra_body thinking config is a contradictory
        # second control (litellm raises UnsupportedParamsError on a
        # thinking_level+budget conflict). Reject at load so the operator picks
        # exactly one.
        extra_body = self.kwargs.get("extra_body")
        if isinstance(extra_body, dict) and (
            "thinking_config" in extra_body or "thinkingConfig" in extra_body
        ):
            raise ValueError(
                f"models cannot set both reasoning_effort and an extra_body "
                f"thinking config (model={self.model!r}); reasoning_effort "
                f"already maps to the native thinking budget — set only one."
            )

    @classmethod
    def from_config(cls, value: object) -> "ModelSpec":
        """Parse a reyn.yaml models: entry into a ModelSpec.

        Accepted forms:
          str  -> ModelSpec(model=value, kwargs={})
          dict -> model field is required; remaining fields go to kwargs.
                  ``extends`` is stripped (handled by ModelResolver, not here).

        Raises:
          ValueError  if value is a dict without a ``model`` key, or if
                      value is neither str nor dict.
        """
        if isinstance(value, str):
            return cls(model=value, kwargs={})
        if isinstance(value, dict):
            if "model" not in value:
                raise ValueError(
                    "ModelSpec dict form requires a 'model' key; "
                    f"got keys: {list(value.keys())}"
                )
            model = value["model"]
            if not isinstance(model, str):
                raise ValueError(
                    f"ModelSpec 'model' must be a string, got {type(model).__name__}"
                )
            # #309: api_base / provider are explicit routing fields, not litellm
            # passthrough kwargs. (No api_key: literal secrets never in config —
            # litellm reads the key from the standard provider env.)
            api_base = value.get("api_base")
            provider = value.get("provider")
            if api_base is not None and not isinstance(api_base, str):
                raise ValueError(
                    f"ModelSpec 'api_base' must be a string, got {type(api_base).__name__}"
                )
            if provider is not None and not isinstance(provider, str):
                raise ValueError(
                    f"ModelSpec 'provider' must be a string, got {type(provider).__name__}"
                )
            kwargs = {
                k: v for k, v in value.items()
                if k not in ("model", "extends", "api_base", "provider")
            }
            return cls(model=model, kwargs=kwargs, api_base=api_base, provider=provider)
        raise ValueError(
            f"ModelSpec.from_config expects str or dict, got {type(value).__name__}"
        )


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    - When both base[k] and override[k] are dicts, recurse.
    - Otherwise override[k] replaces base[k] (including lists and scalars).
    - Keys present only in base are carried unchanged.
    - Keys present only in override are added.
    """
    result = dict(base)
    for key, override_val in override.items():
        base_val = result.get(key)
        if isinstance(base_val, dict) and isinstance(override_val, dict):
            result[key] = _deep_merge(base_val, override_val)
        else:
            result[key] = override_val
    return result


def model_family(model: str) -> str:
    """#1791 A2: coarse model-family classifier for SP gating (the SINGLE place
    family is classified — do NOT scatter ``"gemini" in x`` across SP builders).
    Model names are not skill-specific strings, so this is P7-OK. Operates on the
    resolved LiteLLM model string (e.g. ``"openai/gemini-2.5-flash-lite"`` →
    ``"gemini"`` — the family is in the name regardless of the proxy prefix).
    Returns one of ``"claude" | "gemini" | "gpt" | "other"``.
    """
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return "claude"
    if "gemini" in m or "gemma" in m:
        return "gemini"
    if "gpt" in m or "codex" in m or "/o1" in m or "/o3" in m:
        return "gpt"
    return "other"


def _spec_to_dict(spec: ModelSpec) -> dict[str, Any]:
    """Convert a ModelSpec back to a flat dict for merging."""
    result: dict[str, Any] = {"model": spec.model}
    result.update(spec.kwargs)
    return result


def _strip_keys(d: dict, keys: set[str]) -> dict:
    """Return a copy of *d* with the given *keys* removed."""
    return {k: v for k, v in d.items() if k not in keys}


def resolve_purpose_class(
    explicit: "str | None", resolver: "ModelResolver | None", purpose: str,
) -> str:
    """#1672: pick the model CLASS for a *purpose*, with explicit-wins precedence.

    An ``explicit`` (caller-supplied) value wins; otherwise the *resolver*'s
    per-purpose class (``class_for_purpose``, which falls back to the configured
    default class); otherwise ``"standard"`` when no config-aware resolver is
    available (a stub / no-resolver context — byte-identical to the former
    hardcodes). Shared by ``RouterLoop`` (router) + the planner so the chat router
    and plan-decomposition router resolve identically from one place.
    """
    if explicit is not None:
        return explicit
    if resolver is not None and hasattr(resolver, "class_for_purpose"):
        return resolver.class_for_purpose(purpose)
    return "standard"


class ModelResolver:
    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        builtin: dict[str, dict] | None = None,
        default_class: str = "standard",
        purpose_classes: dict[str, str] | None = None,
    ) -> None:
        """Build a ModelResolver.

        Args:
            mapping:  User-declared models from reyn.yaml (``ReynConfig.models``).
            builtin:  Built-in catalog.  Defaults to ``BUILTIN_MODELS``.
                      User entries in *mapping* override entries with the same
                      name.  Pass ``{}`` to disable built-ins (useful in tests).
            default_class: #1672 — the configured default model class
                      (``ReynConfig.model``) returned by ``class_for_purpose`` for
                      any unset purpose. Defaults to ``"standard"`` so resolvers
                      built without it stay byte-identical to the old hardcodes.
            purpose_classes: #1672 — per-purpose class overrides
                      (``ReynConfig.model_class_by_purpose``). A purpose present
                      here wins over ``default_class`` in ``class_for_purpose``.
        """
        if builtin is None:
            builtin = BUILTIN_MODELS

        self._default_class = default_class
        self._purpose_classes: dict[str, str] = dict(purpose_classes or {})

        # Flat namespace: user entries override built-ins.
        self._namespace: dict[str, Any] = {**builtin, **mapping}

        # Resolve all entries at startup (fail-fast).
        self._resolved: dict[str, ModelSpec] = {}
        for name in self._namespace:
            if name not in self._resolved:
                self._resolved[name] = self._resolve_entry(
                    name, self._namespace[name], frozenset()
                )

        # #1454 PR-B: name-position validation. The resolved ``model`` is a NAME
        # position, which should be ``provider/model`` (the `/`-prefix invariant
        # — all builtin defaults comply). WARN (not error) for a bare name:
        # litellm may accept some bare strings, so bare usage is
        # degraded-but-allowed, flagged so a misroute is diagnosable. (Class
        # positions — tier references — are closed-world via
        # resolve_class_or_fallback; this is the name-position leg of the same
        # unified class/name rule, shared with embedding.classes[*].model.)
        for _name, _spec in self._resolved.items():
            if "/" not in _spec.model:
                import logging

                logging.getLogger(__name__).warning(
                    "models.%s model %r has no provider prefix ('/') — a model "
                    "position should be 'provider/model' (e.g. 'openai/gpt-4o'). "
                    "Treating as a bare LiteLLM name; add the prefix if "
                    "resolution misroutes.",
                    _name, _spec.model,
                )

    def resolve(self, name: str) -> ModelSpec:
        """Return the ModelSpec for name. Pass through as a no-kwargs ModelSpec if not in namespace."""
        if name in self._resolved:
            return self._resolved[name]
        # Unknown name: passthrough — the name IS the LiteLLM model string.
        return ModelSpec(model=name, kwargs={})

    def class_for_purpose(self, purpose: str) -> str:
        """#1672: the model CLASS for a logical call *purpose* (router / control_ir
        / tool / compaction / judge).

        A per-purpose override (``model_class_by_purpose``) wins; otherwise the
        configured default class (``ReynConfig.model``). This is the OS-side mirror
        of ``ReynConfig.model_class_for`` for the many call sites that hold a
        threaded ``ModelResolver`` but not the ``ReynConfig`` object. Explicit
        per-call selections (op.model / frontmatter) are applied by the caller
        BEFORE this fallback and still win. Returns a CLASS name — feed it through
        ``resolve()`` to get the ModelSpec / litellm string.
        """
        return self.purpose_class_or(purpose, self._default_class)

    def purpose_class_or(self, purpose: str, default: str) -> str:
        """#1679: the per-purpose class override if one is configured, else
        *default* — a caller-supplied fallback.

        Unlike ``class_for_purpose`` (whose fallback is the resolver's configured
        ``default_class`` = ``ReynConfig.model``), this lets a caller keep its OWN
        existing model source when no override is set. The CompactionEngine sites
        need exactly this: their model source today is the chat session's model
        (``self.model``) / the plan router model, which can diverge from
        ``default_class`` under an ``SkillRuntime(model=…)`` override — so feeding
        ``default_class`` would silently move compaction OFF the agent's chosen
        model. With ``default`` = the site's existing source, wiring is
        byte-identical until ``model_class_by_purpose.compaction`` is set, at which
        point the documented key takes effect (was a dead key — #1679). Returns a
        CLASS name — feed it through ``resolve()`` to get the litellm string.
        """
        return self._purpose_classes.get(purpose, default)

    def is_known_class(self, name: str) -> bool:
        """Return True if name is a configured model class (i.e. present in the namespace).

        Use this to distinguish intentional model-class overrides (e.g. "standard",
        "light") from literal LiteLLM strings that may have been injected by the LLM
        and are not in the proxy configuration.
        This prevents LLM-hallucinated model strings from bypassing the proxy.
        """
        return name in self._resolved

    def known_classes(self) -> list[str]:
        """Return a sorted list of all known model class names (user-configured +
        built-ins). Use for user-facing display (e.g. ``/model`` no-arg) and for
        validation error messages — single public surface, avoids callers reaching
        into ``_resolved`` directly."""
        return sorted(self._resolved)

    def resolve_class_or_fallback(
        self, requested: str | None, fallback: str | None, *, where: str = "",
    ) -> str:
        """Resolve a CLASS-TYPED model selection (op/skill-supplied) closed-world.

        #1454 PR-B (the unified class/name rule): a class-typed position is
        closed-world — a ``requested`` value that is NOT a known class is NOT
        passed through as a literal LiteLLM model (that would let a
        skill-authored / LLM-injected string bypass the proxy config, which is
        the single source of truth for model selection). Returns ``requested``
        only when it is a known class; otherwise logs ONE decision-enabling
        warning and returns the trusted ``fallback``.

        This is the "standard gate" promotion of :meth:`is_known_class` — every
        op/skill-supplied model field (``op.model``) routes through here rather
        than each call site re-implementing the guard. Operator-config model
        references (``cfg.model`` etc., from reyn.yaml) deliberately do NOT use
        this gate: literal LiteLLM strings there are an intentional
        backward-compat passthrough (see :meth:`resolve`).
        """
        if requested and not self.is_known_class(requested):
            import logging

            logging.getLogger(__name__).warning(
                "%s: model %r is not a known model class — ignoring and using "
                "%r instead. Use a model class (e.g. light / standard / strong, "
                "or one defined in reyn.yaml models:) so the proxy config stays "
                "the single source of truth.",
                where or "model selection", requested, fallback or "standard",
            )
            return fallback or "standard"
        return requested or fallback or "standard"

    # ------------------------------------------------------------------
    # Internal resolution helpers
    # ------------------------------------------------------------------

    def _resolve_entry(
        self, name: str, value: Any, seen: frozenset[str]
    ) -> ModelSpec:
        """Resolve a single namespace entry, following extends chains.

        Args:
            name:   The class name being resolved (used in cycle / error messages).
            value:  The raw value from the namespace (str or dict).
            seen:   Names already on the current resolution stack (cycle detection).

        Returns:
            Fully resolved ModelSpec.

        Raises:
            ValueError: cycle detected, unknown reference, or missing ``model`` field.
        """
        if name in seen:
            chain = " -> ".join(list(seen) + [name])
            raise ValueError(f"circular extends detected: {chain}")

        seen = seen | {name}

        if isinstance(value, str):
            if "/" in value:
                # Literal LiteLLM model string (backward compat).
                return ModelSpec(model=value, kwargs={})
            else:
                # Class reference shorthand: treat as {extends: value}.
                return self._resolve_reference(value, seen)

        if isinstance(value, dict):
            extends_target = value.get("extends")
            if extends_target is not None:
                # Resolve base, then deep-merge override on top.
                base_spec = self._resolve_reference(str(extends_target), seen)
                override = _strip_keys(value, {"extends", "model"})
                base_dict = _spec_to_dict(base_spec)
                merged = _deep_merge(base_dict, override)
                # dict form may also override the model string explicitly.
                if "model" in value:
                    merged["model"] = value["model"]
                if "model" not in merged:
                    raise ValueError(
                        f"ModelSpec for '{name}' is missing a 'model' field "
                        f"after resolving extends chain"
                    )
                model = merged.pop("model")
                return ModelSpec(model=model, kwargs=merged)
            else:
                # Plain dict form (no extends): use from_config.
                return ModelSpec.from_config(value)

        raise ValueError(
            f"Namespace entry '{name}' must be str or dict, "
            f"got {type(value).__name__}"
        )

    def _resolve_reference(self, target: str, seen: frozenset[str]) -> ModelSpec:
        """Look up *target* in the namespace and resolve it.

        Raises:
            ValueError: if *target* is not in the namespace.
        """
        if target not in self._namespace:
            raise ValueError(
                f"Unknown model class reference '{target}'. "
                f"Available classes: {sorted(self._namespace.keys())}"
            )
        # If already cached, return directly (avoids redundant recursion for shared bases).
        if target in self._resolved:
            return self._resolved[target]
        resolved = self._resolve_entry(target, self._namespace[target], seen)
        # Cache mid-resolution to avoid double-resolving shared bases.
        self._resolved[target] = resolved
        return resolved
