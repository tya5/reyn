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

# #1682 #3 (R1): ReynConfig references every section config via default_factory,
# AND config_schema.walk_config_schema does get_type_hints(ReynConfig), resolving
# the string forward-refs against THIS module's namespace — so these MUST be
# CONCRETE (non-TYPE_CHECKING) imports or the fields silently drop from the schema.
from reyn.config.chat import (
    ChatConfig,
    CompactionConfig,
    CostWarnConfig,
    LoopConfig,
    OffloadConfig,
    OnLimitConfig,
    ReasoningConfig,
    RenderTemplateConfig,
    SafetyConfig,
    TimeoutConfig,
)
from reyn.config.embedding import (
    ActionRetrievalConfig,
    EmbeddingConfig,
)
from reyn.config.execution import (
    ToolUseConfig,
)
from reyn.config.infra import (
    AgentConfig,
    AuthConfig,
    CronConfig,
    DelegationConfig,
    EventsConfig,
    FsWatchConfig,
    LLMConfig,
    SandboxConfig,
)
from reyn.config.media import (
    MultimodalConfig,
    VoiceConfig,
    WebConfig,
)
from reyn.config.observability import (
    ObservabilityConfig,
)
from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


def _empty_external_transports():
    """Lazy import shim for the default ``ExternalTransportRouting``.

    Avoids importing ``reyn.runtime.external_routing`` at module-load time
    (= ``reyn.config`` is imported very early; the chat-side import
    would create a cycle).
    """
    from reyn.runtime.external_routing import ExternalTransportRouting
    return ExternalTransportRouting()


@dataclass
class ReynConfig:
    model: str = field(
        default="standard",
        metadata={"desc": "Default model class used when a phase has no model_class."},
    )
    # Optional. None = user did not configure; downstream callers decide
    # how to handle (chat router skips the language directive in its
    # system prompt; absent means the router defaults to "ja" preserving the
    # Japanese-enterprise default). Setting an
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
    # #1829: LLM-layer config — the litellm.Router resilience surface
    # (llm.router.*: use / num_retries / fallbacks / cooldown_time /
    # allowed_fails). Default OFF (router.use=False → direct litellm.acompletion).
    llm: LLMConfig = field(
        default_factory=LLMConfig,
        metadata={"desc": "LLM-layer config (litellm.Router resilience: llm.router.*)."},
    )
    # #1672: per-purpose model-class override. The mapping from a logical call
    # purpose (router / control_ir / tool / judge) to a model CLASS was
    # hardcoded in code (router="light", control_ir/tool="standard"), so the
    # user could set what a class resolves to but NOT which class each purpose
    # uses — the owner's "don't do things users can't customize" complaint. This
    # map exposes it: an UNSET purpose falls back to ``model`` (the configured
    # main), so by default routing follows the configured model — no hidden
    # cheaper tier. Setting e.g. ``router: light`` is the explicit opt-in to the
    # cheap per-turn router. Explicit per-call model selection (phase frontmatter
    # model_class) still wins over this fallback.
    #
    # #3785: ``compaction`` is NOT a valid key here anymore — compaction always
    # follows the conversation's active model (no separate purpose override was
    # ever kept in sync with a ``/model`` switch; see
    # ``Session._rebuild_derived_model_engines_for_model``). A config that still
    # sets ``model_class_by_purpose.compaction`` fails to load
    # (``_build_model_class_by_purpose`` below) rather than silently ignoring
    # or warning — the key's presence is itself evidence of a wrong belief
    # ("compaction runs on a separate model") that a warning would leave intact.
    model_class_by_purpose: dict[str, str] = field(
        default_factory=dict,
        metadata={"desc": (
            "Per-purpose model class override (router / control_ir / tool / "
            "judge). Unset purpose → the `model` default. `compaction` is not "
            "a valid key (#3785) — compaction always follows the conversation "
            "model."
        )},
    )
    # LiteLLM proxy: non-secret base URL only.
    # API keys must be set as environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
    # — never stored in config files.
    api_base: str = field(
        default="",
        metadata={"desc": "LiteLLM proxy base URL. Set this if you route requests through a local proxy."},
    )
    # Pre-declared permissions (same structure as phase frontmatter). Values are
    # "allow" (pre-approve, skip the interactive prompt) or "deny" (block outright).
    # Example: permissions: {exec: allow, file.delete: deny, mcp: {github: allow}}
    # (#3226 Phase 3: the `exec` tool's pre-approval key was renamed from `shell`
    # — clean break, no alias; existing reyn.yaml `shell:` keys must be renamed.)
    permissions: dict = field(
        default_factory=dict,
        metadata={"desc": "Pre-declare `allow`/`deny` for specific Control IR ops, skipping the interactive prompt."},
    )
    # MCP server definitions.  Merged across config sources (servers dict is shallow-merged;
    # local overrides project which overrides global).
    #
    # Per-server schema (raw dict; no dataclass — kept flexible so new MCP SDK
    # transport options can be added without OS changes per P7):
    #   `type`:  "stdio" | "http" | "sse"   (required; transport selector)
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
    #         `type`: http
    #         url: https://api.githubcopilot.com/mcp/
    #         headers:
    #           Authorization: "Bearer ${GITHUB_TOKEN}"
    #           X-API-Version: "2024-01-01"
    mcp: dict = field(default_factory=dict)
    # FP-0016 Component E — agent identity for audit trail + HTTP header
    # propagation. Default `reyn/<hostname>` when reyn.yaml has no
    # `agent:` block. Read by Session to construct its EventLog and
    # by mcp_client.MCPClient for the X-Reyn-Agent-Id header.
    agent: AgentConfig = field(default_factory=AgentConfig)
    # #2081 — cross-agent delegation policy. ``delegation.capability_default``
    # (inherit|deny, default=inherit) selects the capability floor an UNBOUND
    # delegated agent receives. Default ``inherit`` = byte-identical to pre-#2081.
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    # FP-0016 Component C — OAuth provider configurations for
    # `reyn auth login`. Empty by default; operator declares providers
    # in reyn.yaml `auth.providers.<name>`.
    auth: "AuthConfig" = field(default_factory=AuthConfig)
    # Chat-session settings (compaction, etc.)
    chat: ChatConfig = field(default_factory=ChatConfig)
    # Audit-log rotation policy (PR20).
    events: EventsConfig = field(default_factory=EventsConfig)
    # Downstream observability export (OTLP/OpenTelemetry). Opt-in + off by
    # default: no `observability.otel.endpoint` (and no OTEL_EXPORTER_OTLP_ENDPOINT
    # env) → the OtelExporter is never built and behavior is byte-identical to
    # having no OTEL. A lossy downstream — never touches the durable events/WAL.
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    # Budget / rate-limit policy (PR22).
    cost: CostConfig = field(default_factory=CostConfig)
    # #1593 — chat-layer tool-use scheme x transport selector. Default
    # scheme=enumerate-all, transport=tool_calls (#1657). FP-0066 P4b split
    # the former single ``chat`` name into the ``scheme`` (presentation) x
    # ``transport`` (how actions are expressed) 2-axis surface, clean-break
    # (#3247) — this is orthogonal to ``action_retrieval.
    # universal_wrappers_enabled``, which is a live presentation sub-flag of
    # the universal-category scheme (catalog-wrapper vs direct-tool) — NOT
    # retired by this selector. (#2768 removed the dead step/phase layers.)
    tool_use: ToolUseConfig = field(default_factory=ToolUseConfig)
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
    # agents implicitly inherit.
    #
    # Default (``None``) auto-resolves the cross-tool standard:
    # ``AGENTS.md`` (the convention Claude Code / Codex / opencode / etc.
    # all read) if present, else ``REYN.md`` (legacy fallback) — so a new
    # project works with AGENTS.md out of the box, an existing REYN.md
    # project keeps working, and a project shared with another tool shares
    # the same AGENTS.md source.
    #
    # An explicit value pins one path (e.g. ``"CLAUDE.md"`` to reuse that
    # source); ``""`` disables injection entirely.
    project_context_path: str | None = None
    # RAG embedding settings (ADR-0033 Phase 1). Default-completed: usable
    # without any reyn.yaml edits after `pip install reyn` + OPENAI_API_KEY.
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    # FP-0004/0005: unified namespace for stop conditions.
    # safety.loop.* and safety.timeout.* replace the legacy limits: /
    # multi_agent: / cost.router_invocations_per_turn keys that were
    # removed in this refactor. safety: is the single source of truth.
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    # #1830 / FP-0052: high-cost model pre-selection awareness.
    cost_warn: CostWarnConfig = field(default_factory=CostWarnConfig)
    # tool-result-schema-redesign §5 / opt-in flip (owner): the tool-result size
    # gates (text token cap + structured inline cap + media follow-up budget) are
    # OFF by default; set ``offload.enabled: true`` to opt in.
    offload: OffloadConfig = field(default_factory=OffloadConfig)
    # FP-0055 / #2679: operator-tunable output bounds for the render_template op
    # (max_output_chars / wall_clock_seconds). Default → the safe in-handler bounds.
    render_template: RenderTemplateConfig = field(default_factory=RenderTemplateConfig)
    # FP-0022 follow-up: declarative SSL config for web_fetch + MCP registry.
    # Priority: web.fetch.ca_bundle → web.fetch.verify_ssl → SSL_VERIFY env →
    # litellm.ssl_verify → SSL_CERT_FILE → True (default).
    web: WebConfig = field(default_factory=WebConfig)
    # Issue #364 — multi-modal cluster: cap binary media size (= images from
    # web_fetch / read_file / MCP) + iv-gated user permission when exceeded.
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)
    # FP-0017: sandbox backend selection + unsupported-platform policy.
    # Default: auto-select the best available backend for this platform.
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    # #1800 slice 5b: the raw ``hooks:`` block (a list of hook entries). Kept raw
    # here and parsed via ``load_hooks`` at Session construction. Empty (default)
    # → empty registry → the HookDispatcher is a no-op.
    hooks: list = field(default_factory=list)
    # Hook-Event Redesign Phase 4b/5 (proposal 0059 §5/§9, #2880/#2881): the raw
    # ``composers:`` block (a list of Composer definitions). Kept raw here and
    # parsed via ``reyn.hooks.composer.load_composers`` at Session construction
    # (the startup/OUT-set layer of the same 4-layer additive combine ``hooks:``
    # uses). Empty (default) → no composers → ``start_composers`` is never
    # called → byte-identical to pre-Composer behavior.
    composers: list = field(default_factory=list)
    # FP-0034: universal catalog gating + action retrieval (D13 / D14).
    # Default-off so existing chat behaviour is byte-identical until the
    # operator explicitly opts in; will flip in PR-3b-iii after LLMReplay
    # fixtures are re-recorded.
    action_retrieval: "ActionRetrievalConfig" = field(
        default_factory=lambda: ActionRetrievalConfig(),
    )
    # FP-0009 Component B — cron-driven scheduled message dispatch.
    # Empty by default; operator declares jobs in reyn.yaml ``cron.jobs``.
    cron: CronConfig = field(default_factory=CronConfig)
    # #2608 H4 — operator-declared filesystem watch paths -> ``file_changed``
    # external-event hook. Empty by default (paths=[]) → the session-owned
    # FsWatcher never starts (byte-identical to pre-H4). OUT-set only (restart-
    # only, reyn.yaml/reyn.local.yaml) — see FsWatchConfig's docstring for why
    # this must never be an agent-settable / hot-reloadable surface.
    fs_watch: FsWatchConfig = field(default_factory=FsWatchConfig)
    # FP-0041 #489 PR-D2 — external chat transport routing (= Slack /
    # LINE / Discord etc.). Empty by default; operator declares
    # transport → MCP tool mapping in reyn.yaml ``external_transports``.
    # See ``reyn.runtime.external_routing.ExternalTransportRouting``.
    external_transports: "ExternalTransportRouting" = field(
        default_factory=lambda: _empty_external_transports(),
    )
    # #2548 PR-A: skill registry config. Raw dict passed to
    # reyn.data.skills.registry.build_skill_registry at session /
    # router construction. Shape:
    #   skills:
    #     entries:
    #       <name>:
    #         path: "skills/foo/SKILL.md"
    #         description: "One-line description"
    #         enabled: true
    #         visibility: menu   # menu | on_demand | hidden (#2971)
    # `visibility` names which discovery surface the skill reaches: `menu` =
    # the L1 system-prompt Skills menu; `on_demand` = the skill_list tool only
    # (no standing token cost); `hidden` = no model-facing surface. `enabled:
    # false` dominates it — the entry is dropped from the registry outright, so
    # the pair describes 4 states, not 6. The removed `auto_invoke` key is
    # rejected at load by loader._validate_skill_visibility.
    # Merged across config tiers by name (explicit entries win on collision).
    skills: dict = field(default_factory=dict)
    # Pipeline registry config. Raw dict passed to
    # reyn.data.pipelines.registry.build_pipeline_registry at session-factory
    # time (SessionFactoryConfig.from_config). Pipelines are registered PURELY
    # via explicit ``pipelines.entries`` declarations — the same registration
    # model as ``skills.entries`` / ``mcp.servers`` (clean break: the prior
    # directory-scan model — ``pipelines.scan_dirs`` + a blind glob over a
    # ``pipelines/`` dir — is removed; a *.yaml file with no config entry is
    # invisible to every session). Shape:
    #   pipelines:
    #     entries:
    #       <key>:
    #         path: "pipelines/hello.yaml"
    #         description: "One-line description"   # optional
    #         enabled: true                          # optional, default true
    # Each entry's ``path`` is parsed via ``parse_pipeline_docs`` (a file may
    # hold multiple ``pipeline:`` documents — #2722). Namespacing is always on:
    # every pipeline registers as ``{key}.{declared-name}``. The config entry
    # key is a pure namespace label (it need NOT equal any declared name); a
    # dot-less ``call``/``match`` target resolves to a same-file sibling, a
    # dotted one to a global — see ``build_pipeline_registry``.
    # Absent/empty → no pipelines loaded. Merged across config tiers by name
    # (explicit entries win on collision) — same union-merge shape as
    # ``skills`` (see ``_merge`` in loader.py).
    pipelines: dict = field(default_factory=dict)
    # FP-0054 PR-C: named-presentation-template registry config. Raw dict passed to
    # reyn.data.presentations.registry.build_presentation_registry at session-factory
    # time (SessionFactoryConfig.from_config). A named template's value is a
    # blueprint (the same declarative component tree an inline `present` blueprint
    # is), validated at build time. Registering a named template is an
    # OPERATOR/config action (write-gate culture — the LLM authors inline blueprints
    # only, never registers named templates), so there is no install op. Shape:
    #   presentations:
    #     entries:
    #       <name>:
    #         blueprint:                              # required; inline component tree
    #           - component: table
    #             rows: {"$bind": "/results"}
    #             columns:
    #               - {header: Author, path: /author}
    #         description: "One-line description"     # optional
    #         enabled: true                            # optional, default true
    # Merged across config tiers by name (explicit entries win on collision) — same
    # union-merge shape as ``skills`` / ``pipelines`` (see ``_merge`` in loader.py).
    presentations: dict = field(default_factory=dict)

    # #4194: the policy-tier unknown/renamed config-key COUNT
    # (``loader._warn_unknown_config_keys``'s return value, attached here
    # after construction). `schema_internal` because this is the OPPOSITE
    # of an operator-settable key — it is a runtime-computed FACT about
    # the config the operator wrote, not something the operator writes
    # themselves; `walk_config_schema` must never advertise it as a
    # `reyn.yaml` key (`reyn config set unknown_config_key_count: 5` would
    # be nonsense). Read by `Session.unknown_config_key_count`, which is
    # what the interactive CUI's bottom chrome reads — the warning this
    # counts previously only ever reached a log file
    # (`_setup_interactive_logging` redirects all logs there), invisible
    # to the operator. Scope: policy tier only (`reyn.yaml`/
    # `reyn.local.yaml`/`~/.reyn/config.yaml`) — matches `reyn config
    # validate`'s own scope exactly, so the indicator's "run reyn config
    # validate" guidance is always answerable by that command. The
    # hot-reload IN-set (`.reyn/*.yaml`) has its OWN separate unknown-key
    # warn path (`hot_reload._warn_unknown_hot_reload_keys`) that this
    # count does NOT include — that remaining silence is real and is
    # tracked separately (#4235), not covered by this field.
    unknown_config_key_count: int = field(
        default=0, metadata={"schema_internal": True},
    )

    def model_class_for(self, purpose: str) -> str:
        """#1672: the model CLASS for a logical call *purpose*.

        A per-purpose override in ``model_class_by_purpose`` wins; otherwise the
        configured default class ``model`` (so unset purposes follow the user's
        configured model — no hidden cheaper tier). Explicit per-call selections
        ( ``op.model``, phase frontmatter ``model_class``) are applied by
        the caller BEFORE this fallback and still win.
        """
        return self.model_class_by_purpose.get(purpose, self.model)


# #1672: the logical purposes whose model class is configurable via
# ``model_class_by_purpose``. A typo'd key would silently never apply (the call
# sites look up fixed keys), so the parser warns on an unknown key rather than
# hard-failing (forward-compatible — a future purpose key is a warn, not a crash).
# #3785: ``compaction`` deliberately excluded — see
# ``_build_model_class_by_purpose``'s dedicated (hard-failing) handling below,
# not the generic unknown-key warn path.
MODEL_CLASS_PURPOSES: frozenset[str] = frozenset({
    "router", "control_ir", "tool", "judge",
})


def _build_model_class_by_purpose(raw: object) -> dict[str, str]:
    """#1672: parse ``model_class_by_purpose`` (purpose → model class). Unknown
    purpose keys WARN (not error) — a typo would silently never apply, so flag it
    decision-enablingly while staying forward-compatible with future purposes.

    #3785: ``compaction`` is the ONE exception — it used to be a valid key
    (removed) and its presence is not a typo but a stale belief ("compaction
    runs on its own model"), which a warning would leave uncorrected (the
    config keeps silently doing nothing every session). Refuses to load
    instead, with the remedy in the message.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k)
        if key == "compaction":
            raise ValueError(
                "model_class_by_purpose.compaction is no longer configurable "
                "(#3785) — compaction always follows the conversation's "
                "active model now (it did not track a `/model` switch before, "
                "which this removal fixes). Remove this key from your "
                "reyn.yaml/reyn.local.yaml."
            )
        if key not in MODEL_CLASS_PURPOSES:
            import logging
            logging.getLogger(__name__).warning(
                "model_class_by_purpose.%s is not a known purpose %s — it will "
                "never be applied; check for a typo.",
                key, sorted(MODEL_CLASS_PURPOSES),
            )
        out[key] = str(v)
    return out
