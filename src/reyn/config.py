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

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


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
    """`limits:` — central place for runtime bounds (timeouts, retries, caps).

    Note (FP-0004): the user-facing schema preferred for new configs is
    ``safety:`` (= ``safety.loop`` + ``safety.timeout``), which groups by the
    *reason Reyn might stop* rather than by internal component. The
    ``LimitsConfig`` dataclass remains the canonical internal carrier so
    runtime / skill-runner code keeps working unchanged; the loader fills
    both ``safety`` and ``limits`` from whichever key the user wrote.
    """
    llm: LLMLimitsConfig = field(default_factory=LLMLimitsConfig)
    phase: PhaseLimitsConfig = field(default_factory=PhaseLimitsConfig)


# ── FP-0004: safety: section (user-facing unified schema) ──────────────────


@dataclass
class LoopConfig:
    """`safety.loop:` — caps that catch repetitive / runaway behaviour.

    These are *loop-detection* limits (= "the agent is doing the same thing
    over and over"). Hitting one of these is normal during exploratory
    development; raising the cap is the right operator response when the
    workload genuinely needs more iterations.

    Fields:
        max_act_turns_per_phase:
            Global default for the per-phase ``max_act_turns`` (= LLM ↔ op
            volleys inside one phase visit). Skill / phase frontmatter still
            wins when set. ``0`` = unlimited.
        max_phase_visits:
            How many times any single phase may be entered in one skill run.
            Maps to ``LimitsConfig.phase.max_visits``.
        max_router_calls_per_turn:
            Cap on chat-router invocations within a single user turn.
            Maps to ``CostConfig.router_invocations_per_turn``. ``0`` = unlimited.
        max_agent_hops:
            Maximum delegation depth (= user → A → B → C is 3 hops).
            Maps to ``MultiAgentConfig.max_hop_depth``.
        max_skill_calls_per_chain:
            Cap on skill spawns per (chain, skill) pair, surfaced via
            ``CostConfig.per_chain_skill_calls.hard_limit``. ``None`` =
            unlimited (= matches the existing default).
    """

    max_act_turns_per_phase: int = 10
    max_phase_visits: int = 25
    max_router_calls_per_turn: int = 3
    max_agent_hops: int = 3
    max_skill_calls_per_chain: int | None = None


@dataclass
class TimeoutConfig:
    """`safety.timeout:` — wall-clock bounds.

    These are *timeout* limits (= "this is taking too long"). Hitting one
    almost always means a slow LLM, a stuck delegation, or an unbounded
    loop in user code. Raise the cap when the workload legitimately needs
    longer; investigate when it shouldn't.

    Fields:
        llm_call_seconds:
            Per-call timeout passed to ``litellm.acompletion``. Maps to
            ``LimitsConfig.llm.timeout``.
        llm_max_retries:
            Transient-error retry budget per call. Maps to
            ``LimitsConfig.llm.max_retries``.
        phase_seconds:
            Soft wall-clock budget for one phase visit. ``0`` = unlimited.
            Maps to ``LimitsConfig.phase.max_wall_seconds``.
        chain_seconds:
            How long a multi-agent pending chain waits for a delegate
            reply before the runtime synthesises an upstream error. Maps
            to ``MultiAgentConfig.chain_timeout_seconds``. ``0`` (or any
            non-positive value) disables.
    """

    llm_call_seconds: float = 60.0
    llm_max_retries: int = 3
    phase_seconds: float = 0.0
    chain_seconds: float = 60.0


@dataclass
class SafetyConfig:
    """`safety:` — unified, user-facing namespace for stop conditions.

    Reyn stops a run for one of three reasons: a loop was detected, a
    timeout fired, or the budget was exceeded. The first two are grouped
    under ``safety.loop`` / ``safety.timeout``; budget caps stay under
    ``cost:`` because they are financial knobs (per-agent / daily /
    monthly token + USD limits) rather than runaway-detection knobs.

    See ``docs/guide/for-skill-authors/understand-why-reyn-stops.md`` for
    the operator's mental model.

    Backward compatibility (FP-0004): the loader reads both this
    ``safety:`` section and the legacy ``limits:`` / ``multi_agent:`` /
    ``cost.router_invocations_per_turn`` / ``cost.per_chain_skill_calls``
    keys. New keys win when set; old keys provide fallback when new keys
    are missing. Old keys remain functional through the next major
    version.
    """

    loop: LoopConfig = field(default_factory=LoopConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)


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
class VoiceConfig:
    """`voice:` — chat TUI voice-input (Whisper) settings.

    Lazy-loaded only when the user presses the record key (Ctrl+R) so the
    optional deps (`sounddevice`, `faster-whisper`) stay opt-in. See the
    user guide at `docs/guide/for-skill-authors/enable-voice-input.md`.

    Defaults reflect Reyn's Japanese-enterprise focus (project_reyn_vision):
    `language="ja"` so short clips don't get auto-detected as a wrong
    language and produce empty transcripts. Set `language: ""` (empty
    string) or `null` in YAML to opt back into auto-detect.
    """
    enabled: bool = True              # set False to hard-disable Ctrl+R even if deps installed
    model: str = "small"              # tiny | base | small | medium | large-v3
    language: str | None = "ja"       # ISO code; "" or null in YAML = auto-detect
    device: str = "cpu"               # cpu | cuda  (faster-whisper has no metal backend
                                      # — "auto" silently picks the wrong thing on
                                      # some Mac setups, so default to explicit cpu)
    compute_type: str = "int8"        # int8 | float16 | float32
    sample_rate: int = 16000          # Whisper expects 16 kHz mono
    cpu_threads: int = 4              # 0 = OpenMP default (= os.cpu_count()); pinning
                                      # to 4 on Mac avoids the OpenMP/Python-threading
                                      # deadlock seen with high core counts on Apple
                                      # Silicon. Override per-machine if needed.
    num_workers: int = 1              # parallel transcribe streams; we only ever run
                                      # one at a time, so 1 keeps memory + threads low
    max_duration_s: float = 300.0     # auto-cancel recordings longer than this
                                      # (= 5 min default). Prevents runaway memory
                                      # growth + multi-GB transcribe calls if the
                                      # user walks away mid-recording. 16 kHz mono
                                      # float32 ≈ 64 KB/s, so 5 min is ~19 MB.


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


@dataclass
class EmbeddingClassSpec:
    """A single class entry under ``embedding.classes``.

    Mirrors ModelSpec for embedding endpoints. Supports str
    (``'openai/text-embedding-3-small'``) or dict (``{model: '...',
    api_base: '${VAR}', extra_body: {...}}``) form in YAML.
    ``extends`` is resolved at parse time and not stored here.

    ADR-0033 Phase 1 — ``reyn.yaml`` ``embedding:`` section.
    """

    model: str                                      # canonical "<provider>/<name>"
    api_base: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


#: Built-in defaults for ``embedding.classes``.
#: Applied when the section is absent or ``classes:`` is empty.
#: Satisfies the "pip install + OPENAI_API_KEY = works" requirement.
_DEFAULT_EMBEDDING_CLASSES: dict[str, EmbeddingClassSpec] = {
    "light":    EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "standard": EmbeddingClassSpec(model="openai/text-embedding-3-small"),
    "strong":   EmbeddingClassSpec(model="openai/text-embedding-3-large"),
}


@dataclass
class EmbeddingConfig:
    """`embedding:` — RAG embedding settings (ADR-0033 Phase 1).

    Built-in defaults cover the common OpenAI path so users can start
    indexing after ``pip install reyn`` + ``OPENAI_API_KEY`` with no
    ``reyn.yaml`` changes required.

    Fields:
        default_class: Name of the class used when callers don't specify one.
        classes:       Named embedding class → EmbeddingClassSpec mapping.
        batch_size:    Texts per embedding API call (1–2048).
        max_concurrent_batches:
                       Parallel batch calls in flight (1–10).
                       Phase 1 forces 1; values > 1 are accepted but
                       logged as warnings until the concurrent path lands.
        max_retries:   Transient-error retries (0–10).
        retry_backoff: Backoff strategy: ``'exponential'`` or ``'linear'``.
        tokenizer:     tiktoken encoding used for chunk-size estimation.
        cost_warn_threshold:
                       Ask-user gate fires when estimated chunk count
                       exceeds this value (UX gap fix B, ADR-0033 §2.1).
    """

    default_class: str = "standard"
    classes: dict[str, EmbeddingClassSpec] = field(
        default_factory=lambda: dict(_DEFAULT_EMBEDDING_CLASSES)
    )
    batch_size: int = 100
    max_concurrent_batches: int = 1
    max_retries: int = 3
    retry_backoff: Literal["exponential", "linear"] = "exponential"
    tokenizer: str = "cl100k_base"
    cost_warn_threshold: int = 10000

    def resolve_class(self, name: str) -> EmbeddingClassSpec:
        """Look up a class by name; raise ``KeyError`` if unknown."""
        return self.classes[name]


def _parse_embedding_classes(raw: dict[str, Any]) -> dict[str, EmbeddingClassSpec]:
    """Parse the ``embedding.classes`` dict.

    Each entry may be a str (shorthand model name) or a dict with at
    least a ``model`` key. Dict entries support a shallow ``extends``
    lookup within the same raw classes dict (one level only — cycles
    are not checked; multi-level chains are a phase-2 concern).

    Raises:
        ValueError: unknown extends target, missing ``model``, or
                    entry value that is neither str nor dict.
    """
    result: dict[str, EmbeddingClassSpec] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            result[name] = EmbeddingClassSpec(model=value)
        elif isinstance(value, dict):
            if "extends" in value:
                base_name = value["extends"]
                base = raw.get(base_name)
                if isinstance(base, str):
                    base_dict: dict[str, Any] = {"model": base}
                elif isinstance(base, dict):
                    base_dict = {k: v for k, v in base.items() if k != "extends"}
                else:
                    raise ValueError(
                        f"embedding.classes.{name} extends '{base_name}' "
                        f"which doesn't exist in embedding.classes"
                    )
                # Override: base fields replaced by explicit values (extends stripped).
                merged: dict[str, Any] = {
                    **base_dict,
                    **{k: v for k, v in value.items() if k != "extends"},
                }
            else:
                merged = dict(value)
            if "model" not in merged:
                raise ValueError(
                    f"embedding.classes.{name} is missing the required 'model' field"
                )
            result[name] = EmbeddingClassSpec(
                model=str(merged["model"]),
                api_base=(str(merged["api_base"]) if merged.get("api_base") is not None else None),
                extra_body=dict(merged.get("extra_body") or {}),
            )
        else:
            raise ValueError(
                f"embedding.classes.{name} must be a str or dict, "
                f"got {type(value).__name__}"
            )
    return result


def _build_embedding_config(raw: object) -> EmbeddingConfig:
    """Parse the ``embedding:`` section. Empty / missing returns full defaults.

    Validation rules (raise ``ValueError`` on violation):
      - batch_size: 1–2048
      - max_concurrent_batches: 1–10
      - max_retries: 0–10
      - retry_backoff: ``'exponential'`` or ``'linear'``
      - default_class must be a key in the resolved classes dict

    ``${VAR}`` interpolation is already applied to *raw* by the top-level
    loader (ADR-0030) — no special handling is needed here.
    """
    import logging

    if not isinstance(raw, dict):
        return EmbeddingConfig(classes=dict(_DEFAULT_EMBEDDING_CLASSES))

    raw_classes = raw.get("classes") or {}
    if not isinstance(raw_classes, dict):
        raw_classes = {}

    classes = _parse_embedding_classes(raw_classes) if raw_classes else dict(_DEFAULT_EMBEDDING_CLASSES)

    defaults = EmbeddingConfig()
    batch_size = int(raw.get("batch_size", defaults.batch_size))
    max_concurrent_batches = int(raw.get("max_concurrent_batches", defaults.max_concurrent_batches))
    max_retries = int(raw.get("max_retries", defaults.max_retries))
    retry_backoff = str(raw.get("retry_backoff", defaults.retry_backoff))
    tokenizer = str(raw.get("tokenizer", defaults.tokenizer))
    cost_warn_threshold = int(raw.get("cost_warn_threshold", defaults.cost_warn_threshold))
    default_class = str(raw.get("default_class", defaults.default_class))

    if not (1 <= batch_size <= 2048):
        raise ValueError(
            f"embedding.batch_size must be 1–2048, got {batch_size}"
        )
    if not (1 <= max_concurrent_batches <= 10):
        raise ValueError(
            f"embedding.max_concurrent_batches must be 1–10, got {max_concurrent_batches}"
        )
    if max_concurrent_batches > 1:
        logging.getLogger(__name__).warning(
            "embedding.max_concurrent_batches=%d is set but concurrent batch "
            "support is not yet active in phase 1; value is accepted and will "
            "take effect when the concurrent path lands.",
            max_concurrent_batches,
        )
    if not (0 <= max_retries <= 10):
        raise ValueError(
            f"embedding.max_retries must be 0–10, got {max_retries}"
        )
    if retry_backoff not in {"exponential", "linear"}:
        raise ValueError(
            f"embedding.retry_backoff must be 'exponential' or 'linear', "
            f"got {retry_backoff!r}"
        )
    if default_class not in classes:
        raise ValueError(
            f"embedding.default_class '{default_class}' is not a key in "
            f"embedding.classes; available: {sorted(classes)}"
        )

    return EmbeddingConfig(
        default_class=default_class,
        classes=classes,
        batch_size=batch_size,
        max_concurrent_batches=max_concurrent_batches,
        max_retries=max_retries,
        retry_backoff=retry_backoff,  # type: ignore[arg-type]
        tokenizer=tokenizer,
        cost_warn_threshold=cost_warn_threshold,
    )


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
    # FP-0004: user-facing unified namespace for stop conditions
    # (safety.loop.* + safety.timeout.*). The loader fills this from the
    # new ``safety:`` keys with fallback to legacy ``limits:`` /
    # ``multi_agent:`` / ``cost.*`` keys, and ALSO back-fills the legacy
    # dataclasses (``limits``, ``multi_agent``, ``cost.router_invocations_per_turn``,
    # ``cost.per_chain_skill_calls.hard_limit``) so existing consumers keep
    # working. Read this when emitting hint messages or surfacing the
    # operator's mental model; consumers that already read ``limits.*`` can
    # continue to do so.
    safety: SafetyConfig = field(default_factory=SafetyConfig)


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


def _warn_legacy_dot_reyn_config(path: Path) -> None:
    """Emit a migration warning if a deprecated <project>/.reyn/config.yaml exists.

    ADR-0031 removed this layer from the 3-layer cascade.  The file is
    intentionally NOT loaded — only a warning is emitted so the user can
    migrate the settings to reyn.local.yaml manually.
    """
    if path.exists():
        print(
            f"reyn: warning: {path} is deprecated (ADR-0031 — 3-layer config cascade). "
            "Settings in this file are no longer loaded. "
            "Migrate to reyn.local.yaml, then delete this file.",
            file=sys.stderr,
        )


def load_config(cwd: Path | None = None) -> ReynConfig:
    """Load and merge config from all sources. CLI flags are applied by the caller."""
    cwd = (cwd or Path.cwd()).resolve()

    # ADR-0030: load ~/.reyn/secrets.env into os.environ before YAML is
    # parsed so that ${VAR} references in any config field resolve correctly.
    from reyn.secrets.loader import load_secrets_to_environ
    load_secrets_to_environ()

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

        # ADR-0031: <project>/.reyn/config.yaml is DEPRECATED (removed from
        # the 3-layer cascade).  Emit a one-time warning if the file exists so
        # users know to migrate.  The file is intentionally NOT loaded.
        _warn_legacy_dot_reyn_config(project_root / ".reyn" / "config.yaml")

    # ADR-0030: apply ${VAR} interpolation across all string fields of the
    # merged config dict.  At this point os.environ already contains values
    # loaded from ~/.reyn/secrets.env (see load_secrets_to_environ() above).
    from reyn.secrets.interpolation import expand_env
    merged = expand_env(merged)

    raw_ol = merged.get("output_language")
    output_language: str | None
    if isinstance(raw_ol, str) and raw_ol.strip():
        output_language = raw_ol.strip()
    else:
        # Includes the case where the key is missing entirely AND the
        # case where the user explicitly set output_language to "" or
        # null in yaml (= "I want the OS to not pin a language").
        output_language = None
    # FP-0004: parse the new ``safety:`` section first, then build the
    # legacy dataclasses with values that prefer ``safety.*`` when set,
    # falling back to ``limits.*`` / ``multi_agent.*`` / ``cost.*`` for
    # backward compat. This way existing reference sites (which read the
    # legacy fields) keep working while new operators get a unified
    # ``safety:`` namespace.
    safety_raw = merged.get("safety") if isinstance(merged.get("safety"), dict) else {}
    safety = _build_safety_config(safety_raw)
    limits = _build_limits_config_with_safety(merged.get("limits"), safety, safety_raw)
    multi_agent = _build_multi_agent_config_with_safety(
        merged.get("multi_agent"), safety, safety_raw,
    )
    cost = _build_cost_config_with_safety(merged.get("cost"), safety, safety_raw)
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
        limits=limits,
        mcp=dict(merged.get("mcp") or {}),
        python=_build_python_config(merged.get("python")),
        chat=_build_chat_config(merged.get("chat")),
        multi_agent=multi_agent,
        events=_build_events_config(merged.get("events")),
        cost=cost,
        skill_resume=_build_skill_resume_config(merged.get("skill_resume")),
        plan_resume_raw=(
            merged.get("plan_resume")
            if isinstance(merged.get("plan_resume"), dict) else None
        ),
        voice=_build_voice_config(merged.get("voice")),
        embedding=_build_embedding_config(merged.get("embedding")),
        safety=safety,
    )


def _build_voice_config(raw: object) -> VoiceConfig:
    """Parse `voice:` block. Unknown keys are ignored; bad types fall back to defaults.

    ``language`` semantics:
      - omitted          → defaults.language (= "ja")
      - explicit string  → that ISO code
      - "" / null in YAML → ``None`` (= Whisper auto-detect)
    """
    defaults = VoiceConfig()
    if not isinstance(raw, dict):
        return defaults
    if "language" in raw:
        lang_raw = raw["language"]
        if lang_raw is None:
            lang: str | None = None
        elif isinstance(lang_raw, str):
            lang = lang_raw.strip() or None
        else:
            lang = defaults.language
    else:
        lang = defaults.language
    return VoiceConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        model=str(raw.get("model", defaults.model)),
        language=lang,
        device=str(raw.get("device", defaults.device)),
        compute_type=str(raw.get("compute_type", defaults.compute_type)),
        sample_rate=int(raw.get("sample_rate", defaults.sample_rate)),
        cpu_threads=int(raw.get("cpu_threads", defaults.cpu_threads)),
        num_workers=int(raw.get("num_workers", defaults.num_workers)),
        max_duration_s=float(raw.get("max_duration_s", defaults.max_duration_s)),
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
    # FP-0003: opt-in user-approval flow on hard-limit hit.
    ask_on_exceed = bool(raw.get("ask_on_exceed", False))
    extension_calls_raw = raw.get("extension_calls", 0)
    try:
        extension_calls = int(extension_calls_raw)
    except (TypeError, ValueError):
        extension_calls = 0
    if extension_calls < 0:
        extension_calls = 0
    return CostLimitConfig(
        hard_limit=hard,
        warn_ratio=warn_ratio,
        ask_on_exceed=ask_on_exceed,
        extension_calls=extension_calls,
    )


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


# ── FP-0004: safety: section parsers ───────────────────────────────────────


def _build_safety_config(raw: object) -> SafetyConfig:
    """Parse the user-facing ``safety:`` section.

    Empty / missing returns full defaults. Unknown / malformed values
    fall back to defaults silently — config-level errors should not
    abort startup (logger.warning is the convention used elsewhere).
    """
    if not isinstance(raw, dict):
        return SafetyConfig()
    loop_raw = raw.get("loop") or {}
    if not isinstance(loop_raw, dict):
        loop_raw = {}
    timeout_raw = raw.get("timeout") or {}
    if not isinstance(timeout_raw, dict):
        timeout_raw = {}

    loop_defaults = LoopConfig()
    timeout_defaults = TimeoutConfig()

    # ``max_skill_calls_per_chain`` is special-cased: None = unlimited.
    skill_calls_raw = loop_raw.get("max_skill_calls_per_chain", None)
    skill_calls: int | None
    if skill_calls_raw is None:
        skill_calls = None
    else:
        try:
            skill_calls = int(skill_calls_raw)
            if skill_calls < 0:
                skill_calls = None
        except (TypeError, ValueError):
            skill_calls = None

    loop = LoopConfig(
        max_act_turns_per_phase=int(loop_raw.get(
            "max_act_turns_per_phase", loop_defaults.max_act_turns_per_phase,
        )),
        max_phase_visits=int(loop_raw.get(
            "max_phase_visits", loop_defaults.max_phase_visits,
        )),
        max_router_calls_per_turn=int(loop_raw.get(
            "max_router_calls_per_turn", loop_defaults.max_router_calls_per_turn,
        )),
        max_agent_hops=int(loop_raw.get(
            "max_agent_hops", loop_defaults.max_agent_hops,
        )),
        max_skill_calls_per_chain=skill_calls,
    )
    timeout = TimeoutConfig(
        llm_call_seconds=float(timeout_raw.get(
            "llm_call_seconds", timeout_defaults.llm_call_seconds,
        )),
        llm_max_retries=int(timeout_raw.get(
            "llm_max_retries", timeout_defaults.llm_max_retries,
        )),
        phase_seconds=float(timeout_raw.get(
            "phase_seconds", timeout_defaults.phase_seconds,
        )),
        chain_seconds=float(timeout_raw.get(
            "chain_seconds", timeout_defaults.chain_seconds,
        )),
    )
    return SafetyConfig(loop=loop, timeout=timeout)


def _has_loop_key(safety_raw: object, key: str) -> bool:
    """True iff ``safety.loop.<key>`` was explicitly set in YAML."""
    if not isinstance(safety_raw, dict):
        return False
    loop = safety_raw.get("loop")
    return isinstance(loop, dict) and key in loop


def _has_timeout_key(safety_raw: object, key: str) -> bool:
    """True iff ``safety.timeout.<key>`` was explicitly set in YAML."""
    if not isinstance(safety_raw, dict):
        return False
    timeout = safety_raw.get("timeout")
    return isinstance(timeout, dict) and key in timeout


def _build_limits_config_with_safety(
    raw: object, safety: SafetyConfig, safety_raw: object,
) -> LimitsConfig:
    """Build ``LimitsConfig`` from legacy ``limits:`` keys, with
    ``safety.loop.max_phase_visits`` / ``safety.timeout.*`` overriding when
    present.

    New keys win when explicitly set; legacy keys provide fallback
    otherwise. This preserves byte-for-byte behaviour for configs that
    only use the old schema.
    """
    legacy = _build_limits_config(raw)

    llm_timeout = (
        safety.timeout.llm_call_seconds
        if _has_timeout_key(safety_raw, "llm_call_seconds")
        else legacy.llm.timeout
    )
    llm_max_retries = (
        safety.timeout.llm_max_retries
        if _has_timeout_key(safety_raw, "llm_max_retries")
        else legacy.llm.max_retries
    )
    phase_max_visits = (
        safety.loop.max_phase_visits
        if _has_loop_key(safety_raw, "max_phase_visits")
        else legacy.phase.max_visits
    )
    phase_max_wall_seconds = (
        safety.timeout.phase_seconds
        if _has_timeout_key(safety_raw, "phase_seconds")
        else legacy.phase.max_wall_seconds
    )
    return LimitsConfig(
        llm=LLMLimitsConfig(timeout=llm_timeout, max_retries=llm_max_retries),
        phase=PhaseLimitsConfig(
            max_visits=phase_max_visits, max_wall_seconds=phase_max_wall_seconds,
        ),
    )


def _build_multi_agent_config_with_safety(
    raw: object, safety: SafetyConfig, safety_raw: object,
) -> MultiAgentConfig:
    """Build ``MultiAgentConfig`` from legacy keys, with
    ``safety.loop.max_agent_hops`` / ``safety.timeout.chain_seconds``
    overriding when present.
    """
    legacy = _build_multi_agent_config(raw)
    max_hop_depth = (
        safety.loop.max_agent_hops
        if _has_loop_key(safety_raw, "max_agent_hops")
        else legacy.max_hop_depth
    )
    chain_timeout = (
        safety.timeout.chain_seconds
        if _has_timeout_key(safety_raw, "chain_seconds")
        else legacy.chain_timeout_seconds
    )
    return MultiAgentConfig(
        max_hop_depth=max_hop_depth,
        chain_timeout_seconds=chain_timeout,
    )


def _build_cost_config_with_safety(
    raw: object, safety: SafetyConfig, safety_raw: object,
) -> CostConfig:
    """Build ``CostConfig`` from legacy ``cost:`` keys, with
    ``safety.loop.max_router_calls_per_turn`` and
    ``safety.loop.max_skill_calls_per_chain`` overriding when present.

    The other ``cost.*`` financial fields (per-agent tokens, daily / monthly
    USD caps, rate limits) are not part of the safety: namespace and
    remain under ``cost:`` exclusively.
    """
    legacy = _build_cost_config(raw)
    router_cap = (
        safety.loop.max_router_calls_per_turn
        if _has_loop_key(safety_raw, "max_router_calls_per_turn")
        else legacy.router_invocations_per_turn
    )
    per_chain_skill_calls = legacy.per_chain_skill_calls
    if _has_loop_key(safety_raw, "max_skill_calls_per_chain"):
        # Only override the hard_limit; preserve any other legacy fields
        # (warn_ratio, ask_on_exceed, extension_calls).
        new_hard: float | None
        if safety.loop.max_skill_calls_per_chain is None:
            new_hard = None
        else:
            new_hard = float(safety.loop.max_skill_calls_per_chain)
        per_chain_skill_calls = CostLimitConfig(
            hard_limit=new_hard,
            warn_ratio=legacy.per_chain_skill_calls.warn_ratio,
            ask_on_exceed=legacy.per_chain_skill_calls.ask_on_exceed,
            extension_calls=legacy.per_chain_skill_calls.extension_calls,
        )
    return CostConfig(
        per_agent_tokens=legacy.per_agent_tokens,
        per_agent_cost_usd=legacy.per_agent_cost_usd,
        per_chain_skill_calls=per_chain_skill_calls,
        per_chain_skill_tokens=legacy.per_chain_skill_tokens,
        rate_limit_per_minute=legacy.rate_limit_per_minute,
        rate_limit_warn_ratio=legacy.rate_limit_warn_ratio,
        router_invocations_per_turn=router_cap,
        daily_tokens=legacy.daily_tokens,
        daily_cost_usd=legacy.daily_cost_usd,
        monthly_tokens=legacy.monthly_tokens,
        monthly_cost_usd=legacy.monthly_cost_usd,
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
