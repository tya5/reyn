"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong.
Mapping is provided by AgentOSConfig.models (loaded from agent-os.yaml).
Unknown names pass through unchanged (backward compatible with raw LiteLLM strings).
"""
from __future__ import annotations

#: The three standard model tiers. Users should map these in agent-os.yaml.
STANDARD_CLASSES = ("light", "standard", "strong")


class ModelResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def resolve(self, name: str) -> str:
        """Return the LiteLLM model string for name. Pass through if not in mapping."""
        return self._mapping.get(name, name)
