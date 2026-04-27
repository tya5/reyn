"""
AgentOS configuration loader.

Priority (lowest → highest):
  built-in defaults
  ~/.agent-os/config.yaml        user global
  <project>/agent-os.yaml        project (git managed)
  <project>/agent-os.local.yaml  local overrides (gitignored)
  CLI flags                      per-invocation

Scalars: higher priority wins outright.
models dict: shallow merge — each key overrides independently.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentOSConfig:
    model: str = "standard"
    workspace: str = "./workspace"
    output_language: str = "ja"
    shell_allowed: bool = False
    models: dict[str, str] = field(default_factory=dict)
    # LiteLLM proxy: non-secret base URL only.
    # API keys must be set as environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
    # — never stored in config files.
    api_base: str = ""


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge(base: dict, override: dict) -> dict:
    """Merge override into base. models dict is shallow-merged; all other keys override."""
    result = dict(base)
    for key, val in override.items():
        if val is None:
            continue
        if key == "models" and isinstance(val, dict):
            result["models"] = {**result.get("models", {}), **val}
        else:
            result[key] = val
    return result


def _find_project_root(start: Path) -> Path | None:
    """Walk up from start until finding agent-os.yaml, or return None."""
    current = start.resolve()
    while True:
        if (current / "agent-os.yaml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(cwd: Path | None = None) -> AgentOSConfig:
    """Load and merge config from all sources. CLI flags are applied by the caller."""
    cwd = (cwd or Path.cwd()).resolve()

    merged: dict = {"model": "standard", "workspace": "./workspace",
                    "output_language": "ja", "shell_allowed": False, "models": {}}

    # User global
    merged = _merge(merged, _load_yaml(Path.home() / ".agent-os" / "config.yaml"))

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        merged = _merge(merged, _load_yaml(project_root / "agent-os.yaml"))
        merged = _merge(merged, _load_yaml(project_root / "agent-os.local.yaml"))

    return AgentOSConfig(
        model=str(merged.get("model", "standard")),
        workspace=str(merged.get("workspace", "./workspace")),
        output_language=str(merged.get("output_language", "ja")),
        shell_allowed=bool(merged.get("shell_allowed", False)),
        models={str(k): str(v) for k, v in (merged.get("models") or {}).items()},
        api_base=str(merged.get("api_base") or ""),
    )
