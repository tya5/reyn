"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong.
Mapping is provided by ReynConfig.models (loaded from reyn.yaml).
Unknown names pass through unchanged (backward compatible with raw LiteLLM strings).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
            kwargs = {k: v for k, v in value.items() if k != "model"}
            return cls(model=model, kwargs=kwargs)
        raise ValueError(
            f"ModelSpec.from_config expects str or dict, got {type(value).__name__}"
        )


class ModelResolver:
    def __init__(self, mapping: dict[str, Any]) -> None:
        # Parse each mapping value into a ModelSpec at construction time.
        # Accepts both legacy str-form and new dict-form values.
        self._specs: dict[str, ModelSpec] = {
            name: ModelSpec.from_config(value)
            for name, value in mapping.items()
        }

    def resolve(self, name: str) -> ModelSpec:
        """Return the ModelSpec for name. Pass through as a no-kwargs ModelSpec if not in mapping."""
        if name in self._specs:
            return self._specs[name]
        # Unknown name: passthrough — the name IS the LiteLLM model string.
        return ModelSpec(model=name, kwargs={})

    def is_known_class(self, name: str) -> bool:
        """Return True if name is a configured model class (i.e. present in the mapping).

        Use this to distinguish intentional model-class overrides (e.g. "standard",
        "light") from literal LiteLLM strings that may have been injected by the LLM
        and are not in the proxy configuration.
        This prevents LLM-hallucinated model strings from bypassing the proxy.
        """
        return name in self._specs
