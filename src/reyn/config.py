"""
Reyn configuration loader.

Priority (lowest → highest):
  built-in defaults
  ~/.reyn/config.yaml         user global
  <project>/reyn.yaml         project (git managed)
  <project>/reyn.local.yaml   local developer overrides (gitignored)
  <project>/.reyn/config.yaml override of overrides (gitignored)
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


# PR22: CostConfig + CostLimitConfig live in `reyn.budget` (re-exported here
# for ReynConfig typing). They include domain logic (warn_threshold etc.)
# that doesn't belong in the config-only module.
from reyn.budget.budget import CostConfig, CostLimitConfig  # noqa: E402


@dataclass
class EventsConfig:
    """`events:` — audit log rotation policy (PR20).

    Chat session events are appended to a folder under
    `.reyn/events/agents/<name>/chat/<YYYY-MM>/` and rotated when either
    the active file's size exceeds `max_bytes` OR its age (or local date)
    exceeds `max_age_seconds`. Setting both to 0 disables rotation, which
    is the mode skill_run uses (1 run = 1 file).

    `cleanup_period_days` documents how long closed files should be kept
    before `reyn events purge` may delete them. `null` (default) disables
    automatic deletion — purge only runs when invoked explicitly. Setting
    `0` is rejected (it is a footgun: Claude Code historically treated
    `0` as "disable transcript writes" and surprised users).
    """
    max_bytes: int = 10 * 1024 * 1024     # 10 MB
    max_age_seconds: int = 24 * 60 * 60   # 1 day
    cleanup_period_days: int | None = None


@dataclass
class MultiAgentConfig:
    """`multi_agent:` — knobs for agent-to-agent messaging (PR11+).

    Inspired by LangGraph's `recursion_limit` — a single hard cap that prevents
    runaway delegation chains. depth=0 is the user-originated request; each
    `_send_to_agent` increments. `max_hop_depth=3` allows up to user→A→B→C
    (3 hops) before refusing further delegation.

    `chain_timeout_seconds` (PR18) bounds how long a pending chain may wait
    for delegate responses before the runtime gives up and synthesizes an
    error response back upstream. `0` (or any non-positive value) disables
    timeouts entirely — useful for tests / experiments where long-running
    delegates are expected.
    """
    max_hop_depth: int = 3
    chain_timeout_seconds: float = 60.0


SKILL_RESUME_POLICIES = ("prompt", "retry", "skip", "discard_skill")


@dataclass
class SkillResumeConfig:
    """`skill_resume:` — policy for handling ambiguous steps on resume.

    An *ambiguous step* is a ``step_started`` WAL event with no matching
    ``step_completed`` / ``step_failed``. The op may have committed
    externally (canonical intermediate-state); only the operator
    can decide what to do.

    Policies (one of ``SKILL_RESUME_POLICIES``):
      - ``retry``         — re-execute the step (default). Safe for
                            read-only ops and for skills the operator
                            trusts to be idempotent. Risk: duplicate
                            side effect.
      - ``skip``          — synthesize an empty / default completion.
                            The skill continues as if the op succeeded
                            without actually running it. Risk: missing
                            data downstream.
      - ``discard_skill`` — abort the entire skill run, drop the
                            checkpoint, surface a failure to the
                            originating chain.
      - ``prompt``        — legacy/no-op under PR-resume-auto. Retained
                            for config compatibility. Treated as
                            ``retry`` by the auto-resume runtime
                            (no interactive prompt is shown — see the
                            R-D3 廃案 note in the active plan).

    ``per_skill`` overrides the default for specific skill names —
    operator declares which skills are safe to retry vs which require
    careful inspection.

    Default changed from ``prompt`` to ``retry`` in PR-resume-auto: the
    auto-resume design never blocks on interactive prompt; ``retry`` is
    the safest non-blocking choice (correct for the common
    flaky-read-API case after PR-memo-purity-fix invalidates world op
    memos on resume).
    """

    default: str = "retry"
    per_skill: dict[str, str] = field(default_factory=dict)

    def policy_for(self, skill_name: str) -> str:
        """Return the resume policy for a given skill name.

        Falls back to ``default`` when no per_skill override exists.
        Caller may further inspect / validate the value (already
        validated to be in ``SKILL_RESUME_POLICIES`` at config-load
        time).
        """
        return self.per_skill.get(skill_name, self.default)


@dataclass
class ReynConfig:
    model: str = "standard"
    # Optional. None = user did not configure; downstream callers decide
    # how to handle (chat router skips the language directive in its
    # system prompt; phase / skill paths default to "ja" preserving the
    # Japanese-enterprise default for skill artifacts). Setting an
    # explicit value (e.g. "ja", "en") forces a strict directive in the
    # chat router prompt — see `_ROUTER_RETRY_EXHAUSTED_MSG` and
    # `build_system_prompt(output_language=...)`.
    output_language: str | None = None
    shell_allowed: bool = False
    models: dict[str, str | dict] = field(default_factory=dict)
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
    # Multi-agent settings (delegation hop limits, etc.)
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)
    # Audit-log rotation policy (PR20).
    events: EventsConfig = field(default_factory=EventsConfig)
    # Budget / rate-limit policy (PR22).
    cost: CostConfig = field(default_factory=CostConfig)
    # Skill resume policy (PR-skill-resume) — how to handle ambiguous
    # steps on restart.
    skill_resume: SkillResumeConfig = field(default_factory=SkillResumeConfig)
    # Plan resume policy (ADR-0023 Phase 2) — how the resume coordinator
    # treats interrupted plan-mode runs on restart. Loaded as a raw dict
    # and parsed lazily by the coordinator (= keeps PlanResumeConfig in
    # the plan/ module rather than coupling config.py to it).
    plan_resume_raw: dict | None = None
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
    if not rel or project_root is None:
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

    # `output_language` intentionally omitted from merged defaults so we
    # can distinguish "user did not configure" (= None, chat router will
    # skip the language directive) from "user explicitly set it" (= str,
    # router prompt enforces it strictly). See `ReynConfig.output_language`.
    merged: dict = {"model": "standard",
                    "shell_allowed": False, "models": {}, "permissions": {},
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
        project_local = _load_yaml(project_root / "reyn.local.yaml")
        _migrate_legacy_keys(project_local, str(project_root / "reyn.local.yaml"))
        merged = _merge(merged, project_local)
        local = _load_yaml(project_root / ".reyn" / "config.yaml")
        _migrate_legacy_keys(local, str(project_root / ".reyn" / "config.yaml"))
        merged = _merge(merged, local)

    raw_ol = merged.get("output_language")
    output_language: str | None
    if isinstance(raw_ol, str) and raw_ol.strip():
        output_language = raw_ol.strip()
    else:
        # Includes the case where the key is missing entirely AND the
        # case where the user explicitly set output_language to "" or
        # null in yaml (= "I want the OS to not pin a language").
        output_language = None
    return ReynConfig(
        model=str(merged.get("model", "standard")),
        output_language=output_language,
        shell_allowed=bool(merged.get("shell_allowed", False)),
        models={
            str(k): (v if isinstance(v, dict) else str(v))
            for k, v in (merged.get("models") or {}).items()
        },
        api_base=str(merged.get("api_base") or ""),
        permissions=dict(merged.get("permissions") or {}),
        limits=_build_limits_config(merged.get("limits")),
        mcp=dict(merged.get("mcp") or {}),
        python=_build_python_config(merged.get("python")),
        chat=_build_chat_config(merged.get("chat")),
        multi_agent=_build_multi_agent_config(merged.get("multi_agent")),
        events=_build_events_config(merged.get("events")),
        cost=_build_cost_config(merged.get("cost")),
        skill_resume=_build_skill_resume_config(merged.get("skill_resume")),
        plan_resume_raw=(
            merged.get("plan_resume")
            if isinstance(merged.get("plan_resume"), dict) else None
        ),
    )


def _build_skill_resume_config(raw: object) -> SkillResumeConfig:
    """Parse `skill_resume:` block; reject unknown policy values up front."""
    defaults = SkillResumeConfig()
    if not isinstance(raw, dict):
        return defaults
    default = str(raw.get("default", defaults.default))
    if default not in SKILL_RESUME_POLICIES:
        # Unknown policy → fall back to default (safe). Don't raise — config
        # parse failures should never block startup; logger.warning is the
        # convention used elsewhere for "bad config keys".
        import logging
        logging.getLogger(__name__).warning(
            "skill_resume.default %r is not one of %s; using %r",
            default, SKILL_RESUME_POLICIES, defaults.default,
        )
        default = defaults.default
    per_skill_raw = raw.get("per_skill") or {}
    per_skill: dict[str, str] = {}
    if isinstance(per_skill_raw, dict):
        for k, v in per_skill_raw.items():
            v_str = str(v)
            if v_str not in SKILL_RESUME_POLICIES:
                import logging
                logging.getLogger(__name__).warning(
                    "skill_resume.per_skill[%r] = %r is not one of %s; "
                    "skipping", k, v_str, SKILL_RESUME_POLICIES,
                )
                continue
            per_skill[str(k)] = v_str
    return SkillResumeConfig(default=default, per_skill=per_skill)


def _build_multi_agent_config(raw: object) -> MultiAgentConfig:
    defaults = MultiAgentConfig()
    if not isinstance(raw, dict):
        return defaults
    return MultiAgentConfig(
        max_hop_depth=int(raw.get("max_hop_depth", defaults.max_hop_depth)),
        chain_timeout_seconds=float(
            raw.get("chain_timeout_seconds", defaults.chain_timeout_seconds)
        ),
    )


def _build_cost_limit(raw: object) -> CostLimitConfig:
    if not isinstance(raw, dict):
        return CostLimitConfig()
    hard = raw.get("hard_limit")
    if hard is not None:
        try:
            hard = float(hard)
        except (TypeError, ValueError):
            hard = None
    warn_ratio = raw.get("warn_ratio", 0.8)
    try:
        warn_ratio = float(warn_ratio)
    except (TypeError, ValueError):
        warn_ratio = 0.8
    return CostLimitConfig(hard_limit=hard, warn_ratio=warn_ratio)


def _build_cost_config(raw: object) -> CostConfig:
    if not isinstance(raw, dict):
        return CostConfig()
    rate_raw = raw.get("rate_limit_per_minute") or {}
    rate: dict[str, int] = {}
    if isinstance(rate_raw, dict):
        for k, v in rate_raw.items():
            try:
                rate[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    warn_ratio = raw.get("rate_limit_warn_ratio", 0.8)
    try:
        warn_ratio = float(warn_ratio)
    except (TypeError, ValueError):
        warn_ratio = 0.8
    router_cap_raw = raw.get("router_invocations_per_turn", 3)
    try:
        router_cap = int(router_cap_raw)
        if router_cap < 0:
            router_cap = 3
    except (TypeError, ValueError):
        router_cap = 3
    return CostConfig(
        per_agent_tokens=_build_cost_limit(raw.get("per_agent_tokens")),
        per_agent_cost_usd=_build_cost_limit(raw.get("per_agent_cost_usd")),
        per_chain_skill_calls=_build_cost_limit(raw.get("per_chain_skill_calls")),
        per_chain_skill_tokens=_build_cost_limit(raw.get("per_chain_skill_tokens")),
        rate_limit_per_minute=rate,
        rate_limit_warn_ratio=warn_ratio,
        router_invocations_per_turn=router_cap,
        # PR25: persistent daily / monthly quota
        daily_tokens=_build_cost_limit(raw.get("daily_tokens")),
        daily_cost_usd=_build_cost_limit(raw.get("daily_cost_usd")),
        monthly_tokens=_build_cost_limit(raw.get("monthly_tokens")),
        monthly_cost_usd=_build_cost_limit(raw.get("monthly_cost_usd")),
    )


def _build_events_config(raw: object) -> EventsConfig:
    defaults = EventsConfig()
    if not isinstance(raw, dict):
        return defaults
    cleanup = raw.get("cleanup_period_days", defaults.cleanup_period_days)
    if cleanup == 0:
        # Reject the Claude-Code-style "0 disables writes" footgun.
        # Use null/None to disable automatic cleanup; positive ints to enable.
        raise ValueError(
            "events.cleanup_period_days=0 is not allowed; "
            "use null to disable automatic cleanup, or a positive int."
        )
    cleanup_val: int | None = None
    if cleanup is not None:
        cleanup_val = int(cleanup)
    return EventsConfig(
        max_bytes=int(raw.get("max_bytes", defaults.max_bytes)),
        max_age_seconds=int(raw.get("max_age_seconds", defaults.max_age_seconds)),
        cleanup_period_days=cleanup_val,
    )
