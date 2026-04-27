"""
ModelResolver: resolves model class names to LiteLLM model strings.

Standard classes: light, standard, strong
User mappings defined in dsl/models.yaml (or any dsl_root/models.yaml).
Unknown names pass through unchanged (backward compatible).
"""
from __future__ import annotations
from pathlib import Path

#: The three standard model tiers. Users should map these in models.yaml.
STANDARD_CLASSES = ("light", "standard", "strong")


class ModelResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def resolve(self, name: str) -> str:
        """Return the LiteLLM model string for name. Pass through if not in mapping."""
        return self._mapping.get(name, name)

    @classmethod
    def load(cls, dsl_root: str | Path | None) -> "ModelResolver":
        """Load mapping from <dsl_root>/models.yaml. Returns identity resolver if not found."""
        if dsl_root is None:
            return cls({})
        path = Path(dsl_root) / "models.yaml"
        if not path.exists():
            return cls({})
        try:
            import yaml
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return cls({})
        if not isinstance(data, dict):
            return cls({})
        return cls({str(k): str(v) for k, v in data.items() if k and v})
