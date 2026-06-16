"""
Reyn configuration loader.

Priority (lowest → highest):
  built-in defaults
  ~/.reyn/config.yaml         user global
  <project>/reyn.yaml         project (git managed)
  <project>/reyn.local.yaml   local developer overrides (gitignored, human + tool)
  CLI flags                   per-invocation

Note: <project>/.reyn/config.yaml was removed in ADR-0031 (3-layer cascade).
  If the file is still present a one-time migration warning is emitted; the
  file is NOT loaded.  Move settings to reyn.local.yaml and delete the file.

Scalars: higher priority wins outright.
models dict: shallow merge — each key overrides independently.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.budget.budget import CostConfig, CostLimitConfig

# #1682 #3 (R1): ReynConfig references every section config via default_factory,
# AND config_schema.walk_config_schema does get_type_hints(ReynConfig), resolving
# the string forward-refs against THIS module's namespace — so these MUST be
# CONCRETE (non-TYPE_CHECKING) imports or the fields silently drop from the schema.
from reyn.config.chat import (
    ChatConfig,
    CompactionConfig,
    LoopConfig,
    OnLimitConfig,
    ReasoningConfig,
    SafetyConfig,
    TimeoutConfig,
)
from reyn.config.embedding import (
    ActionRetrievalConfig,
    EmbeddingConfig,
    SkillSearchConfig,
)
from reyn.config.execution import (
    PlanConfig,
    SelfImprovementConfig,
    SkillResumeConfig,
    TimeTravelConfig,
    ToolUseConfig,
)
from reyn.config.infra import (
    AgentConfig,
    AuthConfig,
    CronConfig,
    EvalConfig,
    EventsConfig,
    PythonConfig,
    SandboxConfig,
)
from reyn.config.media import (
    MultimodalConfig,
    VoiceConfig,
    WebConfig,
)


def _empty_external_transports():
    """Lazy import shim for the default ``ExternalTransportRouting``.

    Avoids importing ``reyn.chat.external_routing`` at module-load time
    (= ``reyn.config`` is imported very early; the chat-side import
    would create a cycle).
    """
    from reyn.chat.external_routing import ExternalTransportRouting
    return ExternalTransportRouting()


@dataclass
class ReynConfig:
    model: str = field(
        default="standard",
        metadata={"desc": "Default model class used when a phase has no model_class."},
    )
    # Optional. None = user did not configure; downstream callers decide
    # how to handle (chat router skips the language directive in its
    # system prompt; phase / skill paths default to "ja" preserving the
    # Japanese-enterprise default for skill artifacts). Setting an
    # explicit value (e.g. "ja", "en") forces a strict directive in the
    # chat router prompt — see `_ROUTER_RETRY_EXHAUSTED_MSG` and
    # `build_system_prompt(output_language=...)`.
    output_language: str | None = field(
        default=None,
        metadata={"desc": "Language code injected into the context frame for all LLM outputs."},
    )
    models: dict[str, str | dict] = field(
        default_factory=dict,
        metadata={"desc": "Map of model class names to LiteLLM model strings."},
    )
    # #1672: per-purpose model-class override. The mapping from a logical call
    # purpose (router / control_ir / tool / compaction / judge) to a model CLASS
    # was hardcoded in code (router="light", control_ir/tool="standard"), so the
    # user could set what a class resolves to but NOT which class each purpose
    # uses — the owner's "don't do things users can't customize" complaint. This
    # map exposes it: an UNSET purpose falls back to ``model`` (the configured
    # main), so by default routing follows the configured model — no hidden
    # cheaper tier. Setting e.g. ``router: light`` is the explicit opt-in to the
    # cheap per-turn router. Explicit per-call selections (run_skill op.model,
    # phase frontmatter model_class) still WIN over this fallback.
    model_class_by_purpose: dict[str, str] = field(
        default_factory=dict,
        metadata={"desc": (
            "Per-purpose model class override (router / control_ir / tool / "
            "compaction / judge). Unset purpose → the `model` default."
        )},
    )
    tool_calls_op_loop_skills: list[str] = field(
        default_factory=list,
        metadata={"desc": (
            "TRANSITIONAL: skill names opted into the native-tools op-loop — the "
            "phase act-loop drives the shared RouterLoop.run_loop (the converged "
            "op-loop, #1092). Skills not listed use the default json-mode execution "
            "path, unchanged. Removed once the op-loop becomes the default. (#1092 "
            "PR-C-3 merged the former separate routerloop_convergence_skills gate "
            "into this one — the converged path is now the op-loop's implementation.)"
        )},
    )
    # LiteLLM proxy: non-secret base URL only.
    # API keys must be set as environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
    # — never stored in config files.
    api_base: str = field(
        default="",
        metadata={"desc": "LiteLLM proxy base URL. Set this if you route requests through a local proxy."},
    )
    # Pre-approved permissions (same structure as phase frontmatter, but value is "allow").
    # Example: permissions: {shell: allow, file.delete: allow, mcp: {github: allow}}
    permissions: dict = field(
        default_factory=dict,
        metadata={"desc": "Pre-approve specific Control IR ops without interactive prompts."},
    )
    # MCP server definitions.  Merged across config sources (servers dict is shallow-merged;
    # local overrides project which overrides global).
    #
    # Per-server schema (raw dict; no dataclass — kept flexible so new MCP SDK
    # transport options can be added without OS changes per P7):
    #   type:    "stdio" | "http" | "sse"   (required; transport selector)
    #   command, args, env, cwd             (stdio transport)
    #   url, headers, timeout               (http / streamable-http transport)
    #
    # ``headers`` is an optional ``dict[str, str]`` of HTTP headers passed at
    # connection time to HTTP-mode MCP servers (FP-0016 Component A). Used
    # for Bearer tokens, API keys, and any other auth / versioning headers
    # the upstream server requires.  Values support ``${VAR}`` env
    # interpolation (ADR-0030) so secrets stay out of yaml — the env vars
    # are sourced from the process environment + ``~/.reyn/secrets.env``.
    #
    # Example:
    #   mcp:
    #     servers:
    #       github:
    #         type: http
    #         url: https://api.githubcopilot.com/mcp/
    #         headers:
    #           Authorization: "Bearer ${GITHUB_TOKEN}"
    #           X-API-Version: "2024-01-01"
    mcp: dict = field(default_factory=dict)
    # FP-0024 Component D — Anthropic tool_search_tool threshold.
    # Number of MCP tools at or above which build_tools() switches from
    # inlining all MCP tool schemas to using Anthropic's tool_search_tool
    # (deferred-loading mode).  Default 30; set 0 to disable.
    # Configurable via ``mcp.search_threshold:`` in reyn.yaml.
    # Spring AI experiment: 63–64% token reduction at 40+ MCP tools.
    #
    # ``schema_internal``: this field is INTERNAL storage derived by the
    # loader from the ``mcp.search_threshold`` key (see
    # ``_parse_mcp_search_threshold``); it is NOT itself an operator-settable
    # top-level key. The operator sets ``mcp.search_threshold`` (a free-form
    # sub-key of the ``mcp`` dict); ``reyn config set mcp_search_threshold``
    # would be a no-op on reload. The metadata flag tells
    # ``walk_config_schema`` to omit it from the settable schema so
    # ``reyn config set/get/fields`` and the doc-mirror guard don't advertise
    # a key the set/get path can't honor.
    mcp_search_threshold: int = field(default=30, metadata={"schema_internal": True})
    # FP-0024 Component A — BM25 skill pre-filter settings.
    # Below threshold: full enum. Above threshold: BM25 top-K filter.
    # Default 20 — current stdlib (~30-50 skills) stays at full enum unless
    # the operator explicitly lowers the threshold.
    skill_search: SkillSearchConfig = field(default_factory=SkillSearchConfig)
    # Python preprocessor step settings.
    python: PythonConfig = field(default_factory=PythonConfig)
    # FP-0016 Component E — agent identity for audit trail + HTTP header
    # propagation. Default `reyn/<hostname>` when reyn.yaml has no
    # `agent:` block. Read by ChatSession to construct its EventLog and
    # by mcp_client.MCPClient for the X-Reyn-Agent-Id header.
    agent: AgentConfig = field(default_factory=AgentConfig)
    # FP-0016 Component C — OAuth provider configurations for
    # `reyn auth login`. Empty by default; operator declares providers
    # in reyn.yaml `auth.providers.<name>`.
    auth: "AuthConfig" = field(default_factory=AuthConfig)
    # Chat-session settings (compaction, etc.)
    chat: ChatConfig = field(default_factory=ChatConfig)
    # Audit-log rotation policy (PR20).
    events: EventsConfig = field(default_factory=EventsConfig)
    # Budget / rate-limit policy (PR22).
    cost: CostConfig = field(default_factory=CostConfig)
    # Skill resume policy (PR-skill-resume) — how to handle ambiguous
    # steps on restart.
    skill_resume: SkillResumeConfig = field(default_factory=SkillResumeConfig)
    # #1582 — time-travel cost knobs. ``time_travel.workspace_capture: false``
    # selects runtime-only rewind (skip the per-boundary shadow-git capture, the
    # largest constant cost). Default-on (full-fidelity rewind). Extensible block.
    time_travel: TimeTravelConfig = field(default_factory=TimeTravelConfig)
    # #1593 — per-layer tool-use scheme selector (chat/step/phase). Default all
    # universal-category (today's behaviour); generalizes universal_wrappers_enabled.
    tool_use: ToolUseConfig = field(default_factory=ToolUseConfig)
    # Plan resume policy (ADR-0023 Phase 2) — how the resume coordinator
    # treats interrupted plan-mode runs on restart. Loaded as a raw dict
    # and parsed lazily by the coordinator (= keeps PlanResumeConfig in
    # the plan/ module rather than coupling config.py to it).
    plan_resume_raw: dict | None = None
    # Voice input (Whisper) settings for the chat TUI. Optional feature gated
    # by the `reyn[voice]` extras; the OS itself never depends on this block.
    voice: VoiceConfig = field(default_factory=VoiceConfig)
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
    # RAG embedding settings (ADR-0033 Phase 1). Default-completed: usable
    # without any reyn.yaml edits after `pip install reyn` + OPENAI_API_KEY.
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    # FP-0004/0005: unified namespace for stop conditions.
    # safety.loop.* and safety.timeout.* replace the legacy limits: /
    # multi_agent: / cost.router_invocations_per_turn keys that were
    # removed in this refactor. safety: is the single source of truth.
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    # FP-0022 follow-up: declarative SSL config for web_fetch + MCP registry.
    # Priority: web.fetch.ca_bundle → web.fetch.verify_ssl → SSL_VERIFY env →
    # litellm.ssl_verify → SSL_CERT_FILE → True (default).
    web: WebConfig = field(default_factory=WebConfig)
    # Issue #364 — multi-modal cluster: cap binary media size (= images from
    # web__fetch / file__read / MCP) + iv-gated user permission when exceeded.
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)
    # FP-0029: plan-mode execution tuning (step iteration budget, etc.)
    plan: PlanConfig = field(default_factory=PlanConfig)
    # FP-0007 Component A: trace export adapter config.
    # Empty exporters list (default) = no export; full backward compat.
    eval: EvalConfig = field(default_factory=EvalConfig)
    # FP-0017: sandbox backend selection + unsupported-platform policy.
    # Default: auto-select the best available backend for this platform.
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    # FP-0006 B+D: skill_improver behavior knobs (on_propose gate + max_versions cap).
    self_improvement: SelfImprovementConfig = field(default_factory=SelfImprovementConfig)
    # FP-0034: universal catalog gating + action retrieval (D13 / D14).
    # Default-off so existing chat behaviour is byte-identical until the
    # operator explicitly opts in; will flip in PR-3b-iii after LLMReplay
    # fixtures are re-recorded.
    action_retrieval: "ActionRetrievalConfig" = field(
        default_factory=lambda: ActionRetrievalConfig(),
    )
    # FP-0009 Component B — cron-driven scheduled skill execution.
    # Empty by default; operator declares jobs in reyn.yaml ``cron.jobs``.
    cron: CronConfig = field(default_factory=CronConfig)
    # FP-0041 #489 PR-D2 — external chat transport routing (= Slack /
    # LINE / Discord etc.). Empty by default; operator declares
    # transport → MCP tool mapping in reyn.yaml ``external_transports``.
    # See ``reyn.chat.external_routing.ExternalTransportRouting``.
    external_transports: "ExternalTransportRouting" = field(
        default_factory=lambda: _empty_external_transports(),
    )

    def model_class_for(self, purpose: str) -> str:
        """#1672: the model CLASS for a logical call *purpose*.

        A per-purpose override in ``model_class_by_purpose`` wins; otherwise the
        configured default class ``model`` (so unset purposes follow the user's
        configured model — no hidden cheaper tier). Explicit per-call selections
        (run_skill ``op.model``, phase frontmatter ``model_class``) are applied by
        the caller BEFORE this fallback and still win.
        """
        return self.model_class_by_purpose.get(purpose, self.model)


# #1672: the logical purposes whose model class is configurable via
# ``model_class_by_purpose``. A typo'd key would silently never apply (the call
# sites look up fixed keys), so the parser warns on an unknown key rather than
# hard-failing (forward-compatible — a future purpose key is a warn, not a crash).
MODEL_CLASS_PURPOSES: frozenset[str] = frozenset({
    "router", "control_ir", "tool", "compaction", "judge",
})


def _build_model_class_by_purpose(raw: object) -> dict[str, str]:
    """#1672: parse ``model_class_by_purpose`` (purpose → model class). Unknown
    purpose keys WARN (not error) — a typo would silently never apply, so flag it
    decision-enablingly while staying forward-compatible with future purposes."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k)
        if key not in MODEL_CLASS_PURPOSES:
            import logging
            logging.getLogger(__name__).warning(
                "model_class_by_purpose.%s is not a known purpose %s — it will "
                "never be applied; check for a typo.",
                key, sorted(MODEL_CLASS_PURPOSES),
            )
        out[key] = str(v)
    return out
