"""
Reyn configuration loader.

Priority (lowest → highest):
  built-in defaults
  ~/.reyn/config.yaml         user global
  <project>/reyn.yaml         project (git managed)
  <project>/.reyn/config.yaml local overrides (gitignored)
  CLI flags                   per-invocation

Scalars: higher priority wins outright.
models dict: shallow merge — each key overrides independently.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReynConfig:
    model: str = "standard"
    state_dir: str = ".reyn"
    output_language: str = "ja"
    shell_allowed: bool = False
    models: dict[str, str] = field(default_factory=dict)
    # LiteLLM proxy: non-secret base URL only.
    # API keys must be set as environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
    # — never stored in config files.
    api_base: str = ""
    # Pre-approved permissions (same structure as phase frontmatter, but value is "allow").
    # Example: permissions: {shell: allow, file.delete: allow, mcp: {github: allow}}
    permissions: dict = field(default_factory=dict)
    # Maximum times any single phase may be visited in one run (0 = unlimited).
    # Prevents infinite rollback/revision loops. Override per-invocation with --max-phase-visits.
    max_phase_visits: int = 25


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
    """Merge override into base. models and permissions dicts are shallow-merged; all other keys override."""
    result = dict(base)
    for key, val in override.items():
        if val is None:
            continue
        if key in ("models", "permissions") and isinstance(val, dict):
            result[key] = {**result.get(key, {}), **val}
        else:
            result[key] = val
    return result


def _find_project_root(start: Path) -> Path | None:
    """Walk up from start until finding reyn.yaml, or return None."""
    current = start.resolve()
    while True:
        if (current / "reyn.yaml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(cwd: Path | None = None) -> ReynConfig:
    """Load and merge config from all sources. CLI flags are applied by the caller."""
    cwd = (cwd or Path.cwd()).resolve()

    merged: dict = {"model": "standard", "state_dir": ".reyn",
                    "output_language": "ja", "shell_allowed": False, "models": {}, "permissions": {},
                    "max_phase_visits": 25}

    # User global
    merged = _merge(merged, _load_yaml(Path.home() / ".reyn" / "config.yaml"))

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        merged = _merge(merged, _load_yaml(project_root / "reyn.yaml"))
        merged = _merge(merged, _load_yaml(project_root / ".reyn" / "config.yaml"))

    return ReynConfig(
        model=str(merged.get("model", "standard")),
        state_dir=str(merged.get("state_dir", ".reyn")),
        output_language=str(merged.get("output_language", "ja")),
        shell_allowed=bool(merged.get("shell_allowed", False)),
        models={str(k): str(v) for k, v in (merged.get("models") or {}).items()},
        api_base=str(merged.get("api_base") or ""),
        permissions=dict(merged.get("permissions") or {}),
        max_phase_visits=int(merged.get("max_phase_visits", 25)),
    )
