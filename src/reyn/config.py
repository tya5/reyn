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
import sys
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
class LLMLimitsConfig:
    """`limits.llm` — bounds on each LLM HTTP call."""
    timeout: float = 60.0    # seconds per call (passed to litellm.acompletion)
    max_retries: int = 3     # transient-error retries (LiteLLM exponential backoff)


@dataclass
class PhaseLimitsConfig:
    """`limits.phase` — bounds applied per phase visit."""
    max_visits: int = 25         # 0 = unlimited
    max_wall_seconds: float = 0  # 0 = unlimited; soft check at retry boundaries


@dataclass
class LimitsConfig:
    """`limits:` — central place for runtime bounds (timeouts, retries, caps)."""
    llm: LLMLimitsConfig = field(default_factory=LLMLimitsConfig)
    phase: PhaseLimitsConfig = field(default_factory=PhaseLimitsConfig)


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
    # Runtime bounds: phase visits, wall-clock budgets, LLM timeouts/retries.
    # Override per-invocation via --max-phase-visits / --phase-budget / --llm-timeout / --llm-max-retries.
    limits: LimitsConfig = field(default_factory=LimitsConfig)
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
        elif key == "limits" and isinstance(val, dict):
            existing = result.get("limits", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_limits = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key in ("llm", "phase") and isinstance(sub_val, dict):
                    merged_limits[sub_key] = {**existing.get(sub_key, {}), **sub_val}
                else:
                    merged_limits[sub_key] = sub_val
            result["limits"] = merged_limits
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


def _build_limits_config(raw: object) -> LimitsConfig:
    if not isinstance(raw, dict):
        return LimitsConfig()
    llm_raw = raw.get("llm") or {}
    if not isinstance(llm_raw, dict):
        llm_raw = {}
    phase_raw = raw.get("phase") or {}
    if not isinstance(phase_raw, dict):
        phase_raw = {}
    llm_defaults = LLMLimitsConfig()
    phase_defaults = PhaseLimitsConfig()
    return LimitsConfig(
        llm=LLMLimitsConfig(
            timeout=float(llm_raw.get("timeout", llm_defaults.timeout)),
            max_retries=int(llm_raw.get("max_retries", llm_defaults.max_retries)),
        ),
        phase=PhaseLimitsConfig(
            max_visits=int(phase_raw.get("max_visits", phase_defaults.max_visits)),
            max_wall_seconds=float(phase_raw.get("max_wall_seconds", phase_defaults.max_wall_seconds)),
        ),
    )


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


def _migrate_legacy_keys(merged: dict, source: str) -> None:
    """Migrate deprecated top-level keys into the `limits:` section in-place.

    Logs a warning to stderr and lifts the legacy value into `limits.phase.max_visits`
    only when the new key is not already set.
    """
    if "max_phase_visits" in merged:
        legacy = merged.pop("max_phase_visits")
        limits = merged.setdefault("limits", {})
        phase = limits.setdefault("phase", {})
        if "max_visits" not in phase:
            phase["max_visits"] = legacy
            print(
                f"reyn: warning: top-level `max_phase_visits` is deprecated ({source}); "
                "use `limits.phase.max_visits` instead.",
                file=sys.stderr,
            )


def load_config(cwd: Path | None = None) -> ReynConfig:
    """Load and merge config from all sources. CLI flags are applied by the caller."""
    cwd = (cwd or Path.cwd()).resolve()

    merged: dict = {"model": "standard", "state_dir": ".reyn",
                    "output_language": "ja", "shell_allowed": False, "models": {}, "permissions": {},
                    "limits": {}, "mcp": {}}

    # User global
    user_global = _load_yaml(Path.home() / ".reyn" / "config.yaml")
    _migrate_legacy_keys(user_global, "~/.reyn/config.yaml")
    merged = _merge(merged, user_global)

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        project = _load_yaml(project_root / "reyn.yaml")
        _migrate_legacy_keys(project, str(project_root / "reyn.yaml"))
        merged = _merge(merged, project)
        local = _load_yaml(project_root / ".reyn" / "config.yaml")
        _migrate_legacy_keys(local, str(project_root / ".reyn" / "config.yaml"))
        merged = _merge(merged, local)

    return ReynConfig(
        model=str(merged.get("model", "standard")),
        state_dir=str(merged.get("state_dir", ".reyn")),
        output_language=str(merged.get("output_language", "ja")),
        shell_allowed=bool(merged.get("shell_allowed", False)),
        models={str(k): str(v) for k, v in (merged.get("models") or {}).items()},
        api_base=str(merged.get("api_base") or ""),
        permissions=dict(merged.get("permissions") or {}),
        limits=_build_limits_config(merged.get("limits")),
        mcp=dict(merged.get("mcp") or {}),
        chat=_build_chat_config(merged.get("chat")),
        python=_build_python_config(merged.get("python")),
    )
