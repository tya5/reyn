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
class CompactionSectionCaps:
    """Per-section token budgets for chat_summary BODY."""
    topic_arc: int = 200
    decisions: int = 400
    pending: int = 400
    session_user_facts: int = 200
    artifacts_referenced: int = 300


@dataclass
class CompactionConfig:
    """`chat.compaction:` — Head/Body/Tail compaction policy.

    See PR4 in /Users/yasudatetsuya/.claude/plans/abstract-knitting-moonbeam.md
    for the design rationale.
    """
    trigger_total_tokens: int = 30000   # Compact when uncovered middle exceeds this
    head_size: int = 12                 # First N user/agent turns kept raw
    tail_size: int = 12                 # Last N user/agent turns kept raw
    body_token_cap: int = 1500          # Total cap across all summary sections
    min_compact_batch: int = 5          # Skip compact when fewer than N turns to absorb
    section_token_caps: CompactionSectionCaps = field(default_factory=CompactionSectionCaps)


@dataclass
class ChatConfig:
    """`chat:` — chat-session-specific runtime knobs."""
    compaction: CompactionConfig = field(default_factory=CompactionConfig)


@dataclass
class ReynConfig:
    model: str = "standard"
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
    # Python preprocessor step settings.
    python: PythonConfig = field(default_factory=PythonConfig)
    # Chat-session settings (compaction, etc.)
    chat: ChatConfig = field(default_factory=ChatConfig)
    # When true, attach Anthropic-style cache_control markers to the system
    # prompt so providers that support prompt caching (Anthropic, AWS Bedrock
    # Claude) can reuse the prefix across calls. Ignored by providers that
    # don't recognize cache_control (Gemini / OpenAI proxies pass-through).
    prompt_cache_enabled: bool = True
    # Path (relative to project root) of a markdown file whose content is
    # injected into the system prompt for every phase. Use this to put
    # project-wide background, conventions, or references somewhere all
    # skills implicitly inherit. Set "" or point to a non-existent file to
    # disable. Default "REYN.md"; users sharing the project with Claude Code
    # may set this to "CLAUDE.md" to reuse the same source.
    project_context_path: str = "REYN.md"


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


def load_project_context(config: ReynConfig, project_root: Path) -> str:
    """Read the project context markdown file referenced by config.project_context_path.

    Returns the file content stripped, or "" when the path is unset, missing,
    or unreadable. Empty / whitespace-only content also yields "" so callers
    can short-circuit the system-prompt section.
    """
    rel = (config.project_context_path or "").strip()
    if not rel:
        return ""
    target = project_root / rel
    if not target.is_file():
        return ""
    try:
        content = target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return content


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
                elif sub_key == "compaction" and isinstance(sub_val, dict):
                    existing_comp = existing.get("compaction") or {}
                    existing_caps = existing_comp.get("section_token_caps") or {}
                    new_caps = sub_val.get("section_token_caps") or {}
                    if isinstance(existing_caps, dict) and isinstance(new_caps, dict):
                        sub_val = {
                            **sub_val,
                            "section_token_caps": {**existing_caps, **new_caps},
                        }
                    merged_chat["compaction"] = {**existing_comp, **sub_val}
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


def _build_python_config(raw: object) -> PythonConfig:
    if not isinstance(raw, dict):
        return PythonConfig()
    modules = raw.get("allowed_modules") or []
    if not isinstance(modules, list):
        modules = []
    return PythonConfig(allowed_modules=[str(m) for m in modules])


def _build_chat_config(raw: object) -> ChatConfig:
    if not isinstance(raw, dict):
        return ChatConfig()
    compaction_raw = raw.get("compaction") or {}
    if not isinstance(compaction_raw, dict):
        return ChatConfig()
    section_raw = compaction_raw.get("section_token_caps") or {}
    if not isinstance(section_raw, dict):
        section_raw = {}
    defaults_section = CompactionSectionCaps()
    section = CompactionSectionCaps(
        topic_arc=int(section_raw.get("topic_arc", defaults_section.topic_arc)),
        decisions=int(section_raw.get("decisions", defaults_section.decisions)),
        pending=int(section_raw.get("pending", defaults_section.pending)),
        session_user_facts=int(
            section_raw.get("session_user_facts", defaults_section.session_user_facts)
        ),
        artifacts_referenced=int(
            section_raw.get("artifacts_referenced", defaults_section.artifacts_referenced)
        ),
    )
    defaults = CompactionConfig()
    compaction = CompactionConfig(
        trigger_total_tokens=int(
            compaction_raw.get("trigger_total_tokens", defaults.trigger_total_tokens)
        ),
        head_size=int(compaction_raw.get("head_size", defaults.head_size)),
        tail_size=int(compaction_raw.get("tail_size", defaults.tail_size)),
        body_token_cap=int(compaction_raw.get("body_token_cap", defaults.body_token_cap)),
        min_compact_batch=int(
            compaction_raw.get("min_compact_batch", defaults.min_compact_batch)
        ),
        section_token_caps=section,
    )
    return ChatConfig(compaction=compaction)


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

    merged: dict = {"model": "standard",
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
        output_language=str(merged.get("output_language", "ja")),
        shell_allowed=bool(merged.get("shell_allowed", False)),
        models={str(k): str(v) for k, v in (merged.get("models") or {}).items()},
        api_base=str(merged.get("api_base") or ""),
        permissions=dict(merged.get("permissions") or {}),
        limits=_build_limits_config(merged.get("limits")),
        mcp=dict(merged.get("mcp") or {}),
        python=_build_python_config(merged.get("python")),
        chat=_build_chat_config(merged.get("chat")),
    )
