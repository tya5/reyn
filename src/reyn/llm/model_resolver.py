"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong.
Mapping is provided by ReynConfig.models (loaded from reyn.yaml).
Unknown names pass through unchanged (backward compatible with raw LiteLLM strings).
"""
from __future__ import annotations

#: The three standard model tiers. Users should map these in reyn.yaml.
STANDARD_CLASSES = ("light", "standard", "strong")


class ModelResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def resolve(self, name: str) -> str:
        """Return the LiteLLM model string for name. Pass through if not in mapping."""
        return self._mapping.get(name, name)

    def is_known_class(self, name: str) -> bool:
        """Return True if name is a configured model class (i.e. present in the mapping).

        Use this to distinguish intentional model-class overrides (e.g. "standard",
        "light") from literal LiteLLM strings that may have been injected by the LLM
        and are not in the proxy configuration.
        """
        return name in self._mapping
