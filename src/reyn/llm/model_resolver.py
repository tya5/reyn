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
            kwargs = {k: v for k, v in value.items() if k not in ("model", "extends")}
            return cls(model=model, kwargs=kwargs)
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


def _spec_to_dict(spec: ModelSpec) -> dict[str, Any]:
    """Convert a ModelSpec back to a flat dict for merging."""
    result: dict[str, Any] = {"model": spec.model}
    result.update(spec.kwargs)
    return result


def _strip_keys(d: dict, keys: set[str]) -> dict:
    """Return a copy of *d* with the given *keys* removed."""
    return {k: v for k, v in d.items() if k not in keys}


class ModelResolver:
    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        builtin: dict[str, dict] | None = None,
    ) -> None:
        """Build a ModelResolver.

        Args:
            mapping:  User-declared models from reyn.yaml (``ReynConfig.models``).
            builtin:  Built-in catalog.  Defaults to ``BUILTIN_MODELS``.
                      User entries in *mapping* override entries with the same
                      name.  Pass ``{}`` to disable built-ins (useful in tests).
        """
        if builtin is None:
            builtin = BUILTIN_MODELS

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

    def is_known_class(self, name: str) -> bool:
        """Return True if name is a configured model class (i.e. present in the namespace).

        Use this to distinguish intentional model-class overrides (e.g. "standard",
        "light") from literal LiteLLM strings that may have been injected by the LLM
        and are not in the proxy configuration.
        This prevents LLM-hallucinated model strings from bypassing the proxy.
        """
        return name in self._resolved

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
