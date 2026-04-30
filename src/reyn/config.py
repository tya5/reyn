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
class ChatMemoryConfig:
    """`chat.memory` section — controls memory recall/extraction in `reyn chat`."""
    enabled: bool = True
    turn_threshold: int = 8         # periodic extract: this many new turns AND
    time_threshold: float = 600.0   # this many seconds since last extract
    recall_top_k: int = 5           # max memories returned per recall


@dataclass
class ChatConfig:
    """`chat` section — settings for `reyn chat`."""
    memory: ChatMemoryConfig = field(default_factory=ChatMemoryConfig)


@dataclass
class PythonConfig:
    """`python` section — settings for the python preprocessor step."""
    # Modules that user code may import in pure mode in addition to the
    # stdlib allowlist. Curate carefully: libraries that internally do I/O
    # (pandas.read_csv, requests, etc.) defeat pure-mode sandboxing.
    allowed_modules: list[str] = field(default_factory=list)


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
    # MCP server definitions.  Merged across config sources (servers dict is shallow-merged;
    # local overrides project which overrides global).
    # Example:
    #   mcp:
    #     servers:
    #       my_tool:
    #         type: http
    #         url: http://localhost:3000/mcp
    #         headers:
    #           Authorization: "Bearer ${MY_TOKEN}"
    mcp: dict = field(default_factory=dict)
    # Chat agent settings (memory thresholds, etc.).
    chat: ChatConfig = field(default_factory=ChatConfig)
    # Python preprocessor step settings.
    python: PythonConfig = field(default_factory=PythonConfig)


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
        elif key == "mcp" and isinstance(val, dict):
            existing = result.get("mcp", {})
            existing_servers = existing.get("servers", {}) if isinstance(existing, dict) else {}
            new_servers = val.get("servers", {}) if isinstance(val, dict) else {}
            result["mcp"] = {**existing, "servers": {**existing_servers, **new_servers}}
        elif key == "chat" and isinstance(val, dict):
            existing = result.get("chat", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_chat = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key == "memory" and isinstance(sub_val, dict):
                    merged_chat["memory"] = {**existing.get("memory", {}), **sub_val}
                else:
                    merged_chat[sub_key] = sub_val
            result["chat"] = merged_chat
        else:
            result[key] = val
    return result


def _build_chat_config(raw: object) -> ChatConfig:
    if not isinstance(raw, dict):
        return ChatConfig()
    mem_raw = raw.get("memory") or {}
    if not isinstance(mem_raw, dict):
        mem_raw = {}
    defaults = ChatMemoryConfig()
    return ChatConfig(memory=ChatMemoryConfig(
        enabled=bool(mem_raw.get("enabled", defaults.enabled)),
        turn_threshold=int(mem_raw.get("turn_threshold", defaults.turn_threshold)),
        time_threshold=float(mem_raw.get("time_threshold", defaults.time_threshold)),
        recall_top_k=int(mem_raw.get("recall_top_k", defaults.recall_top_k)),
    ))


def _build_python_config(raw: object) -> PythonConfig:
    if not isinstance(raw, dict):
        return PythonConfig()
    modules = raw.get("allowed_modules") or []
    if not isinstance(modules, list):
        modules = []
    return PythonConfig(allowed_modules=[str(m) for m in modules])


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
                    "max_phase_visits": 25, "mcp": {}}

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
        mcp=dict(merged.get("mcp") or {}),
        chat=_build_chat_config(merged.get("chat")),
        python=_build_python_config(merged.get("python")),
    )
