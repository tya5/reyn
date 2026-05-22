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


def _default_agent_id() -> str:
    """Compute the default agent_id used when reyn.yaml ``agent.id`` is unset.

    Format: ``reyn/<hostname>``. Pure function so the default is
    inspectable / overridable in tests via the same call site.
    """
    return f"reyn/{socket.gethostname()}"


@dataclass
class AgentConfig:
    """``agent:`` — runtime agent identity for audit trail + HTTP propagation.

    FP-0016 Component E. The ``id`` value is stamped onto every P6 event
    payload (via ``EventLog`` auto-injection) and is added as the
    ``X-Reyn-Agent-Id`` header on outgoing MCP / A2A / external HTTP
    requests. Default ``reyn/<hostname>`` so a fresh install has a usable
    identity without operator action; override in reyn.yaml when running
    multi-agent fleets or enterprise deployments that need a stable
    per-role identifier.
    """

    id: str = field(default_factory=_default_agent_id)


def _build_agent_config(raw: object) -> AgentConfig:
    """Parse ``agent:`` from reyn.yaml.

    ``None`` / missing block / empty dict → default (= ``reyn/<hostname>``).
    Empty string ``id:`` also falls back to default so operators who
    leave the field blank don't end up with an empty agent_id leaking
    into events / headers.
    """
    if raw is None:
        return AgentConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"agent must be a mapping, got {type(raw).__name__}"
        )
    raw_id = raw.get("id")
    if raw_id is None or raw_id == "":
        return AgentConfig()
    if not isinstance(raw_id, str):
        raise ValueError(
            f"agent.id must be a string, got {type(raw_id).__name__}"
        )
    return AgentConfig(id=raw_id)


@dataclass
class AuthConfig:
    """``auth:`` — OAuth provider configurations for `reyn auth login`.

    FP-0016 Component C. Each entry maps a provider name to its OAuth
    2.0 device authorization grant parameters. Empty by default; the
    operator declares providers they want to authenticate against.
    """

    providers: dict[str, "OAuthProviderConfig"] = field(default_factory=dict)


def _build_auth_config(raw: object) -> AuthConfig:
    """Parse ``auth:`` block from reyn.yaml.

    Shape::

        auth:
          providers:
            github:
              client_id: "1234abcd"
              device_authorization_url: "https://github.com/login/device/code"
              token_url: "https://github.com/login/oauth/access_token"
              scopes: ["repo", "user:email"]
              # client_secret: omit for public clients
              # audience: omit for non-Auth0 providers
            google:
              client_id: "...apps.googleusercontent.com"
              device_authorization_url: "https://oauth2.googleapis.com/device/code"
              token_url: "https://oauth2.googleapis.com/token"
              scopes: ["openid", "email"]
              client_secret: "..."

    ``None`` / missing → empty AuthConfig.providers.
    Unknown provider fields are ignored (= forward-compatible).
    """
    from reyn.secrets.oauth import OAuthProviderConfig

    if raw is None:
        return AuthConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"auth must be a mapping, got {type(raw).__name__}"
        )
    raw_providers = raw.get("providers", {}) or {}
    if not isinstance(raw_providers, dict):
        raise ValueError(
            f"auth.providers must be a mapping, got "
            f"{type(raw_providers).__name__}"
        )
    providers: dict[str, OAuthProviderConfig] = {}
    for name, spec in raw_providers.items():
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"auth.providers key must be a non-empty string, got {name!r}"
            )
        if not isinstance(spec, dict):
            raise ValueError(
                f"auth.providers.{name} must be a mapping, got "
                f"{type(spec).__name__}"
            )
        required = ["client_id", "device_authorization_url", "token_url"]
        for k in required:
            if k not in spec:
                raise ValueError(
                    f"auth.providers.{name}: missing required field {k!r}"
                )
            if not isinstance(spec[k], str) or not spec[k]:
                raise ValueError(
                    f"auth.providers.{name}.{k} must be a non-empty string"
                )
        scopes = spec.get("scopes", []) or []
        if not isinstance(scopes, list):
            raise ValueError(
                f"auth.providers.{name}.scopes must be a list of strings"
            )
        client_secret = spec.get("client_secret")
        if client_secret is not None and not isinstance(client_secret, str):
            raise ValueError(
                f"auth.providers.{name}.client_secret must be a string or null"
            )
        audience = spec.get("audience")
        if audience is not None and not isinstance(audience, str):
            raise ValueError(
                f"auth.providers.{name}.audience must be a string or null"
            )
        providers[name] = OAuthProviderConfig(
            name=name,
            client_id=spec["client_id"],
            device_authorization_url=spec["device_authorization_url"],
            token_url=spec["token_url"],
            scopes=[str(s) for s in scopes],
            client_secret=client_secret,
            audience=audience,
        )
    return AuthConfig(providers=providers)


@dataclass
class PythonConfig:
    """`python` section — settings for the python preprocessor step."""
    # Modules that user code may import in pure mode in addition to the
    # stdlib allowlist. Curate carefully: libraries that internally do I/O
    # (pandas.read_csv, requests, etc.) defeat pure-mode sandboxing.
    allowed_modules: list[str] = field(default_factory=list)


# ── FP-0004: safety: section (user-facing unified schema) ──────────────────

# PR22: CostConfig + CostLimitConfig live in `reyn.budget` (re-exported here
# for ReynConfig typing). They include domain logic (warn_threshold etc.)
# that doesn't belong in the config-only module.
from reyn.budget.budget import CostConfig, CostLimitConfig  # noqa: E402


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
            ``0`` = unlimited.
        max_router_calls_per_turn:
            Cap on chat-router invocations within a single user turn.
            ``0`` = unlimited.
        max_agent_hops:
            Maximum delegation depth (= user → A → B → C is 3 hops).
        skill_calls_per_chain:
            Per-(chain, skill) spawn cap with warn + ask_on_exceed semantics.
            ``hard_limit=None`` = unlimited (default).
        skill_tokens_per_chain:
            Per-(chain, skill) token cap with warn semantics.
            ``hard_limit=None`` = unlimited (default).
    """

    max_act_turns_per_phase: int = 10
    max_phase_visits: int = 25
    max_router_calls_per_turn: int = 3
    max_agent_hops: int = 3
    skill_calls_per_chain: CostLimitConfig = field(default_factory=CostLimitConfig)
    skill_tokens_per_chain: CostLimitConfig = field(default_factory=CostLimitConfig)


@dataclass
class TimeoutConfig:
    """`safety.timeout:` — wall-clock bounds.

    These are *timeout* limits (= "this is taking too long"). Hitting one
    almost always means a slow LLM, a stuck delegation, or an unbounded
    loop in user code. Raise the cap when the workload legitimately needs
    longer; investigate when it shouldn't.

    Fields:
        llm_call_seconds:
            Per-call timeout passed to ``litellm.acompletion``.
        llm_max_retries:
            Transient-error retry budget per call.
        phase_seconds:
            Soft wall-clock budget for one phase visit. ``0`` = unlimited.
        chain_seconds:
            How long a multi-agent pending chain waits for a delegate
            reply before the runtime synthesises an upstream error.
            ``0`` (or any non-positive value) disables.
    """

    llm_call_seconds: float = 60.0
    llm_max_retries: int = 3
    phase_seconds: float = 0.0
    chain_seconds: float = 60.0


ON_LIMIT_MODES = ("interactive", "unattended", "auto_extend")


@dataclass
class OnLimitConfig:
    """`safety.on_limit:` — what happens when a loop / timeout limit is hit
    (FP-0005).

    Reyn supports three behaviours when a safety limit fires:

    - ``interactive`` (= default): pause the run, prompt the user via
      ``ask_user`` for permission to continue. On approval the limit
      is extended by one increment; on refusal (or ask timeout) the
      run aborts with ``RunResult.partial_data`` populated. Default
      ``ask_timeout_seconds=0`` means "wait forever for a human
      reply" — silently discarding mid-run state on a 60s wall clock
      is a worse UX than holding the run open until the user returns.

    - ``unattended``: abort immediately on hit. Opt-in for CI / cron
      / scripted runs that genuinely cannot pause for a human, where
      a hung intervention prompt would be a worse outcome than a
      clean abort.

    - ``auto_extend``: auto-extend the limit ``auto_extend_times`` times
      without prompting, then fall through to ``unattended`` behaviour
      once the auto-extend budget is spent. Useful for trusted long-
      running tasks where the operator knows up front that ``N``
      extensions are acceptable.

    The mode applies to the user-facing limits listed in FP-0005 §
    "limit ごとの適用可否" (max_act_turns, max_phase_visits, router_cap,
    skill_calls_per_chain, max_agent_hops, phase_seconds, chain_seconds).
    LLM call timeouts already retry via litellm and are not part of this
    pipeline.

    ``ask_timeout_seconds`` bounds how long ``interactive`` mode waits
    for a user response. ``0`` (= default) means "wait forever";
    positive values abort with ``partial_data`` after the window
    elapses. Headless paths are still safe regardless of timeout:
    ``bus=None`` (= no intervention surface, e.g. dispatch_tool /
    scripted runs) short-circuits to abort via the ``no_bus`` reason
    in ``handle_limit_exceeded``, and ``StdinInterventionBus`` on a
    non-TTY raises ``EOFError`` immediately which the helper treats
    as a refusal.
    """

    mode: Literal["interactive", "unattended", "auto_extend"] = "interactive"
    auto_extend_times: int = 1
    ask_timeout_seconds: float = 0.0


@dataclass
class SafetyConfig:
    """`safety:` — unified, user-facing namespace for stop conditions.

    Reyn stops a run for one of three reasons: a loop was detected, a
    timeout fired, or the budget was exceeded. The first two are grouped
    under ``safety.loop`` / ``safety.timeout``; budget caps stay under
    ``cost:`` because they are financial knobs (per-agent / daily /
    monthly token + USD limits) rather than runaway-detection knobs.

    ``safety.loop.skill_calls_per_chain`` and
    ``safety.loop.skill_tokens_per_chain`` are hybrid caps: they live
    under ``safety.loop`` because they gate repeated skill spawns
    (loop-detection), but carry ``CostLimitConfig`` semantics (warn_ratio,
    ask_on_exceed, extension_calls) because the operator may want the
    user-approval flow on hit rather than an immediate abort.

    See ``docs/guide/for-skill-authors/understand-why-reyn-stops.md`` for
    the operator's mental model.

    ``on_limit`` (FP-0005) controls what happens when a loop / timeout
    limit fires: prompt the user (interactive), abort silently
    (unattended, legacy default), or auto-extend N times then abort
    (auto_extend).
    """

    loop: LoopConfig = field(default_factory=LoopConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    on_limit: OnLimitConfig = field(default_factory=OnLimitConfig)


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


@dataclass
class SkillSearchConfig:
    """`skill_search:` — BM25 skill pre-filter settings (FP-0024 Component A).

    When the catalogue exceeds ``threshold`` skills, the router narrows
    ``invoke_skill.name`` enum to the top-``top_k`` BM25 keyword matches
    before building the tools list.  Falls through to full enum on 0 BM25
    results (= no skill made invisible).

    Fields:
        threshold:  Catalogue size at which BM25 activates. Default 20.
                    Set 0 to always pre-filter; set a high number to disable.
        top_k:      Number of skills returned by BM25. Default 5.
        backend:    ``'bm25'`` (default). ``'embedding'`` / ``'hybrid'``
                    reserved for Component C/D.
    """

    threshold: int = 20
    top_k: int = 5
    backend: str = "bm25"


def _build_skill_search_config(raw: object) -> "SkillSearchConfig":
    """Parse the ``skill_search:`` section. Empty / missing returns defaults."""
    defaults = SkillSearchConfig()
    if not isinstance(raw, dict):
        return defaults
    threshold_raw = raw.get("threshold", defaults.threshold)
    top_k_raw = raw.get("top_k", defaults.top_k)
    backend_raw = raw.get("backend", defaults.backend)
    try:
        threshold = int(threshold_raw)
        if threshold < 0:
            threshold = 0
    except (TypeError, ValueError):
        threshold = defaults.threshold
    try:
        top_k = int(top_k_raw)
        if top_k < 1:
            top_k = 1
    except (TypeError, ValueError):
        top_k = defaults.top_k
    return SkillSearchConfig(
        threshold=threshold,
        top_k=int(top_k),
        backend=str(backend_raw),
    )


@dataclass
class WebFetchConfig:
    """`web.fetch:` — SSL verification settings for web_fetch and MCP registry.

    Priority order (highest to lowest):
      1. ``ca_bundle`` set → use that file path as the CA bundle (``verify=<path>``).
         Satisfies corporate MITM proxy / custom PKI use cases.
      2. ``verify_ssl: false`` → disable SSL verification entirely (``verify=False``).
         Use only in controlled environments where certificate validation is
         intentionally bypassed.
      3. ``verify_ssl: true`` → force SSL verification regardless of env vars.
      4. Both unset (``None``) → fall through to ``SSL_VERIFY`` env var →
         ``litellm.ssl_verify`` → ``SSL_CERT_FILE`` → ``True`` (default).

    Fields:
        verify_ssl:
            ``True`` forces verification. ``False`` disables it. ``None``
            (default) delegates to the env-var / litellm fallback chain.
        ca_bundle:
            Absolute path (or path relative to cwd) of a CA bundle PEM file.
            When set, takes priority over ``verify_ssl`` and env vars.
    """
    verify_ssl: bool | None = None
    ca_bundle: str | None = None


@dataclass
class WebConfig:
    """`web:` — web operation settings.

    Aggregates ``web.fetch`` sub-section. Extend here when ``web.search``
    gets its own knobs.
    """
    fetch: WebFetchConfig = field(default_factory=WebFetchConfig)


def _build_web_fetch_config(raw: object) -> WebFetchConfig:
    """Parse the ``web.fetch:`` sub-section."""
    if not isinstance(raw, dict):
        return WebFetchConfig()
    ca_bundle_raw = raw.get("ca_bundle")
    ca_bundle = str(ca_bundle_raw) if ca_bundle_raw is not None else None
    verify_ssl_raw = raw.get("verify_ssl")
    if verify_ssl_raw is None:
        verify_ssl: bool | None = None
    else:
        verify_ssl = bool(verify_ssl_raw)
    return WebFetchConfig(verify_ssl=verify_ssl, ca_bundle=ca_bundle)


def _build_web_config(raw: object) -> WebConfig:
    """Parse the ``web:`` section. Empty / missing returns full defaults."""
    if not isinstance(raw, dict):
        return WebConfig()
    fetch_raw = raw.get("fetch")
    return WebConfig(fetch=_build_web_fetch_config(fetch_raw))


# ── multimodal: media-size gate for image/audio/etc. (#364 cluster) ─────────


_MULTIMODAL_ON_OVERSIZE = ("ask", "allow", "deny")


@dataclass
class MultimodalConfig:
    """``multimodal:`` — controls how Reyn handles large binary content
    (currently images from web__fetch / file__read / MCP servers) and
    where multimodal artefacts live on disk.

    Fields:
        max_bytes:
            Decoded-payload byte cap before the gate fires. Default 5MB
            matches Anthropic's per-image API limit. Counts the BINARY size
            (= ``len(response.content)`` / ``len(file_bytes)``), not the
            base64-encoded shape.
        on_oversize:
            What to do when a piece of media exceeds ``max_bytes``:

            - ``ask`` (default): prompt the user via the intervention bus
              with size + source info; yes loads the media, no drops it.
            - ``allow``: silently accept; no prompt. Use when running
              non-interactively in a trusted pipeline.
            - ``deny``: silently reject; the op returns ``status="denied"``
              with no media data. Use in cost-sensitive contexts where
              over-limit content should never reach the LLM.
        media_dir:
            Project-relative directory for image binary storage (issue
            #383 PR-C / E-full Phase 3). Files are flat-named with a
            timestamp + chain-id + tool prefix so ``ls -la`` sorts
            chronologically. User-browseable and user-deleteable.
        tool_results_dir:
            Project-relative directory for text-y tool result dumps
            (= #385 PoC foundation). PR-C lands the writer alongside
            ``media_dir``; PR-D wires the consumer + preview.
        base_url:
            Optional canonical URL prefix for cross-host path_ref
            consumption (#385 β core impl sub-task 3b). When set
            (= e.g. ``"https://reyn.example.com"`` from a deployed
            ``reyn web`` instance), ``MediaStore.save_*`` augments the
            path_ref with a ``url`` field pointing at
            ``<base_url>/agents/<agent>/tool-results/<artifact>`` so
            cross-host consumers (= A2A peers, MCP clients, browsers)
            can fetch the body via the resources router. Unset → no
            ``url`` field minted, same-host fast-path only (= backward
            compat for legacy / CLI-only deployments).

    Issue #364 lands this config + the shared ``require_media_load`` gate;
    paths #365 (file__read binary) and #366 (user chat input image) reuse
    them. Issue #383 PR-C extends with the storage paths.
    """
    max_bytes: int = 5_000_000
    on_oversize: Literal["ask", "allow", "deny"] = "ask"
    media_dir: str = ".reyn/media"
    tool_results_dir: str = ".reyn/tool-results"
    base_url: str | None = None


def _build_multimodal_config(raw: object) -> MultimodalConfig:
    """Parse the ``multimodal:`` section. Unknown keys ignored, bad types
    fall back to defaults.
    """
    if not isinstance(raw, dict):
        return MultimodalConfig()
    max_bytes_raw = raw.get("max_bytes")
    try:
        max_bytes = int(max_bytes_raw) if max_bytes_raw is not None else 5_000_000
    except (TypeError, ValueError):
        max_bytes = 5_000_000
    if max_bytes < 0:
        max_bytes = 5_000_000
    on_oversize_raw = raw.get("on_oversize")
    on_oversize: Literal["ask", "allow", "deny"]
    if (
        isinstance(on_oversize_raw, str)
        and on_oversize_raw in _MULTIMODAL_ON_OVERSIZE
    ):
        on_oversize = on_oversize_raw  # type: ignore[assignment]
    else:
        on_oversize = "ask"
    media_dir_raw = raw.get("media_dir")
    media_dir = (
        str(media_dir_raw) if isinstance(media_dir_raw, str) and media_dir_raw
        else ".reyn/media"
    )
    tool_results_dir_raw = raw.get("tool_results_dir")
    tool_results_dir = (
        str(tool_results_dir_raw)
        if isinstance(tool_results_dir_raw, str) and tool_results_dir_raw
        else ".reyn/tool-results"
    )
    base_url_raw = raw.get("base_url")
    base_url: str | None = (
        str(base_url_raw).rstrip("/")
        if isinstance(base_url_raw, str) and base_url_raw
        else None
    )
    return MultimodalConfig(
        max_bytes=max_bytes, on_oversize=on_oversize,
        media_dir=media_dir, tool_results_dir=tool_results_dir,
        base_url=base_url,
    )


SKILL_RESUME_POLICIES = ("prompt", "retry", "skip", "discard_skill")


_SANDBOX_BACKENDS = {"auto", "seatbelt", "landlock", "noop"}
_SANDBOX_ON_UNSUPPORTED = {"warn", "error", "ignore"}


@dataclass
class SelfImprovementConfig:
    """`self_improvement:` — skill_improver behavior knobs (FP-0006).

    Fields:
        on_propose:
            What skill_improver does when it is about to apply improvements
            back to the original skill directory:

            - ``ask_user`` (default): pause and prompt the user via the
              InterventionBus (summarise score + changes, wait for approval
              before writing). Safe default — the user is in the loop.
            - ``auto``: skip the prompt and apply directly. Intended for CI /
              unattended runs where the operator trusts the eval gate.
            - ``disabled``: do NOT apply the changes. Log a
              ``skill_improvement_dry_run`` event noting what would have been
              applied. Useful for "what would improve this skill?" exploration
              without modifying the source.

        max_versions:
            Maximum number of v<N>.md snapshot files kept in
            ``.reyn/skill-versions/<name>/``.  When the cap is exceeded the
            OLDEST version is deleted (the version pointed to by ``current``
            is never deleted).  Default 10.  Set 0 to disable pruning.
    """

    on_propose: Literal["ask_user", "auto", "disabled"] = "ask_user"
    max_versions: int = 10

    def __post_init__(self) -> None:
        _VALID_ON_PROPOSE = {"ask_user", "auto", "disabled"}
        if self.on_propose not in _VALID_ON_PROPOSE:
            raise ValueError(
                f"self_improvement.on_propose {self.on_propose!r} is not one of "
                f"{sorted(_VALID_ON_PROPOSE)}"
            )
        if self.max_versions < 0:
            raise ValueError(
                f"self_improvement.max_versions must be >= 0, got {self.max_versions}"
            )


def _build_self_improvement_config(raw: object) -> "SelfImprovementConfig":
    """Parse the ``self_improvement:`` section. Empty / missing returns defaults."""
    defaults = SelfImprovementConfig()
    if not isinstance(raw, dict):
        return defaults
    on_propose_raw = raw.get("on_propose", defaults.on_propose)
    on_propose = str(on_propose_raw) if on_propose_raw is not None else defaults.on_propose
    max_versions_raw = raw.get("max_versions", defaults.max_versions)
    try:
        max_versions = int(max_versions_raw)
    except (TypeError, ValueError):
        max_versions = defaults.max_versions
    # Validation is delegated to __post_init__ — raises ValueError with clear message.
    return SelfImprovementConfig(on_propose=on_propose, max_versions=max_versions)


@dataclass
class SandboxConfig:
    """`sandbox:` — backend selection and unsupported-platform policy (FP-0017).

    Fields:
        backend:
            Which enforcement backend to use.
            ``'auto'`` (default) lets the OS pick the best available backend
            for the current platform (macOS < 26 → Seatbelt, Linux 5.13+ →
            Landlock, else → Noop). Explicit values force a specific backend.
            Allowed: ``{'auto', 'seatbelt', 'landlock', 'noop'}``.
        on_unsupported:
            Policy when the requested backend is unavailable on this platform.
            ``'warn'`` (default) logs a WARNING and falls back to NoopBackend.
            ``'error'`` raises RuntimeError (useful to fail-fast in enforced
            production environments). ``'ignore'`` silently falls back.
            Allowed: ``{'warn', 'error', 'ignore'}``.
    """

    backend: str = "auto"
    on_unsupported: str = "warn"

    def __post_init__(self) -> None:
        if self.backend not in _SANDBOX_BACKENDS:
            raise ValueError(
                f"sandbox.backend {self.backend!r} is not one of "
                f"{sorted(_SANDBOX_BACKENDS)}"
            )
        if self.on_unsupported not in _SANDBOX_ON_UNSUPPORTED:
            raise ValueError(
                f"sandbox.on_unsupported {self.on_unsupported!r} is not one of "
                f"{sorted(_SANDBOX_ON_UNSUPPORTED)}"
            )


def _build_sandbox_config(raw: object) -> SandboxConfig:
    """Parse the ``sandbox:`` section. Empty / missing returns SandboxConfig()."""
    if not isinstance(raw, dict):
        return SandboxConfig()
    defaults = SandboxConfig()
    backend = str(raw.get("backend", defaults.backend))
    on_unsupported = str(raw.get("on_unsupported", defaults.on_unsupported))
    # Validation delegated to __post_init__ — raises ValueError with clear message.
    return SandboxConfig(backend=backend, on_unsupported=on_unsupported)


@dataclass
class PlanConfig:
    """`plan:` — plan-mode execution tuning.

    ``step_max_iterations``: maximum RouterLoop iterations per plan step
    before the OS records a step failure.  Default 5 (FP-0029).  Raise
    when steps regularly run long tool chains; lower for tighter budgets.

    ``retry_limit``: maximum automatic retries per step on transient errors
    (FP-0031-C).  Default 3.  Set 0 to disable auto-retry.  Exceptions
    that have their own ask/abort path (PermissionError, BudgetExceeded,
    etc.) are always excluded from retry regardless of this setting.
    """
    step_max_iterations: int = 5
    retry_limit: int = 3


@dataclass
class ExporterConfig:
    """`eval.exporters[]:` — a single trace exporter backend declaration.

    Fields:
        type:       ``'file'`` | ``'langfuse'`` | ``'otlp'`` | ``'ietf_audit'``
        path:       Local output dir / file path (file / ietf_audit backends).
        public_key: Langfuse public API key (supports ``${VAR}``).
        secret_key: Langfuse secret API key (supports ``${VAR}``).
        host:       Langfuse base URL (e.g. ``https://cloud.langfuse.com``).
        endpoint:   OTLP collector endpoint (e.g. ``http://localhost:4318``).
    """

    type: str = "file"
    path: str = ".reyn/traces"
    public_key: str = ""
    secret_key: str = ""
    host: str = ""
    endpoint: str = ""


@dataclass
class EvalConfig:
    """`eval:` — trace export configuration (FP-0007 Component A).

    When absent from reyn.yaml the section defaults to an empty exporters
    list (= no export, full backward compatibility).

    Example::

        eval:
          exporters:
            - type: file
              path: .reyn/traces/
            - type: langfuse
              public_key: ${LANGFUSE_PUBLIC_KEY}
              secret_key: ${LANGFUSE_SECRET_KEY}
              host: https://your-langfuse.example.com
            - type: otlp
              endpoint: http://localhost:4317
            - type: ietf_audit
              path: .reyn/audit-trail/
    """

    exporters: list[ExporterConfig] = field(default_factory=list)


def _build_eval_config(raw: object) -> EvalConfig:
    """Parse the ``eval:`` section. Empty / missing returns empty exporters list."""
    if not isinstance(raw, dict):
        return EvalConfig()
    exporters_raw = raw.get("exporters") or []
    if not isinstance(exporters_raw, list):
        return EvalConfig()
    exporters: list[ExporterConfig] = []
    for entry in exporters_raw:
        if not isinstance(entry, dict):
            continue
        exp_type = str(entry.get("type", "file"))
        exporters.append(ExporterConfig(
            type=exp_type,
            path=str(entry.get("path", ".reyn/traces")),
            public_key=str(entry.get("public_key", "")),
            secret_key=str(entry.get("secret_key", "")),
            host=str(entry.get("host", "")),
            endpoint=str(entry.get("endpoint", "")),
        ))
    return EvalConfig(exporters=exporters)


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
class ActionRetrievalConfig:
    """`action_retrieval:` — FP-0034 universal catalog + retrieval settings.

    Phase 1 of FP-0034. The 4 universal wrappers (list_actions /
    search_actions / describe_action / invoke_action) plus the
    qualified-name dispatcher land across PR-1 through PR-3b-iv.
    Subsequent phases extend with hot list / cold start /
    search_actions enablement.

    Fields:
        universal_wrappers_enabled:
            When True (= **default since PR-3b-iv**), ``build_tools()``
            appends the 3 universal wrappers (list_actions /
            describe_action / invoke_action) at the end of tools=.
            ``search_actions`` is gated separately via
            ``embedding_class`` per §D14.

            The flip from False (= PR-3b-i through iii) to True
            happens here in PR-3b-iv. Operators who want to opt out
            (= preserve the prior tools= shape) can set
            ``action_retrieval.universal_wrappers_enabled: false``
            in reyn.yaml.

            Test suite verified safe via FakeRouterHost insulation:
            all LLMReplay fixtures + AsyncMock-based E2E tests do
            NOT implement ``get_universal_wrappers_enabled`` so the
            RouterLoop's getattr fallback keeps tools= shape stable
            for the recorded fixtures. The flip affects production
            runtime only.

        embedding_class:
            Name of the entry in ``embedding.classes`` to use for
            action retrieval semantic search (= §D13). When None or
            empty, ``search_actions`` is excluded from tools= even if
            ``universal_wrappers_enabled`` is True (§D14 gating).

            Phase 1 leaves the embedding index unbuilt; PR-3b+ may
            add it. Setting this field today is harmless (= no
            consumers).

        hot_list_n:
            Hot list size for top-N freq+recency projection (§D2).
            Phase 2 wiring; the field lives here so reyn.yaml shape
            stabilises early. Default sized to cover DEFAULT_HOT_LIST_SEED
            with headroom (= seed length 17 → default 20) so cold-start
            sessions surface every seed entry as a direct alias before
            usage-driven sorting kicks in. Lower values silently truncate
            the seed; the invariant test in test_action_usage_tracker.py
            asserts hot_list_n_default >= len(DEFAULT_HOT_LIST_SEED).

        mode:
            Operational mode label (§D24): ``"minimal"`` /
            ``"default"`` / ``"performance"``. Stored as a free-form
            string so callers can layer interpretations on top
            without further config breaking changes. Default
            ``"default"`` is the §D24 balanced setting.
    """

    universal_wrappers_enabled: bool = True
    embedding_class: str | None = None
    hot_list_n: int = 20
    mode: str = "default"
    # FP-0034 §D16: seed qualified names for initial hot list (before freq
    # accumulates). "default" means the OS-defined 10-item seed (5 universal
    # + 5 Reyn flagship). [] means no seed. Explicit list overrides the
    # default. Parsed by _build_action_retrieval_config.
    hot_list_seed: list[str] | str = "default"


@dataclass
class CronJobConfig:
    """One ``cron.jobs[]`` entry (FP-0009 Component B).

    Maps directly onto ``CronJob`` consumed by ``CronScheduler``; this
    config-side dataclass exists to keep the reyn.yaml parsing layer
    independent of the runtime layer.
    """

    name: str
    skill: str
    schedule: str   # 5-field cron expression
    input: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class CronConfig:
    """``cron:`` — scheduled skill execution (FP-0009 Component B).

    Each entry under ``cron.jobs`` triggers a stdlib or project skill
    on a cron schedule via ``CronScheduler`` (= attached to
    ``app.state.cron_scheduler`` in web mode, or run foreground via
    ``reyn cron run``).
    """

    jobs: list[CronJobConfig] = field(default_factory=list)


def _build_cron_config(raw: object) -> CronConfig:
    """Parse the ``cron:`` section from reyn.yaml.

    Shape::

        cron:
          jobs:
            - name: index_events_hourly
              skill: index_events
              schedule: "0 */6 * * *"
              input: {}
              enabled: true

    ``None`` / missing block / empty dict → ``CronConfig(jobs=[])``.
    Validates ``name``, ``skill``, and ``schedule`` are non-empty strings;
    raises ``ValueError`` naming the offending entry on validation failure.
    Unknown extra fields are ignored (= forward-compatible).
    """
    if raw is None:
        return CronConfig()
    if not isinstance(raw, dict):
        return CronConfig()
    raw_jobs = raw.get("jobs") or []
    if not isinstance(raw_jobs, list):
        return CronConfig()
    jobs: list[CronJobConfig] = []
    for i, entry in enumerate(raw_jobs):
        if not isinstance(entry, dict):
            raise ValueError(
                f"cron.jobs[{i}] must be a mapping, got {type(entry).__name__}"
            )
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(
                f"cron.jobs[{i}]: 'name' must be a non-empty string "
                f"(got {name!r})"
            )
        skill = entry.get("skill")
        if not skill or not isinstance(skill, str):
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): 'skill' must be a non-empty string "
                f"(got {skill!r})"
            )
        schedule = entry.get("schedule")
        if not schedule or not isinstance(schedule, str):
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): 'schedule' must be a non-empty string "
                f"(got {schedule!r})"
            )
        raw_input = entry.get("input") or {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        enabled = bool(entry.get("enabled", True))
        jobs.append(CronJobConfig(
            name=name,
            skill=skill,
            schedule=schedule,
            input=dict(raw_input),
            enabled=enabled,
        ))
    return CronConfig(jobs=jobs)


def _build_action_retrieval_config(raw: object) -> ActionRetrievalConfig:
    """Parse ``action_retrieval:`` from reyn.yaml.

    Accepts a dict with any subset of fields; unknown keys are
    ignored (= forward-compatible with future Phase 2 additions).
    Validates types and clamps numeric ranges to non-negative.

    Raises:
        ValueError: when a recognised field has an invalid type
            (= explicit type mismatch; missing fields fall back to
            defaults).
    """
    if raw is None:
        return ActionRetrievalConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"action_retrieval must be a mapping, got {type(raw).__name__}"
        )

    cfg = ActionRetrievalConfig()

    if "universal_wrappers_enabled" in raw:
        val = raw["universal_wrappers_enabled"]
        if not isinstance(val, bool):
            raise ValueError(
                "action_retrieval.universal_wrappers_enabled must be a bool, "
                f"got {type(val).__name__}"
            )
        cfg.universal_wrappers_enabled = val

    if "embedding_class" in raw:
        val = raw["embedding_class"]
        if val is not None and not isinstance(val, str):
            raise ValueError(
                "action_retrieval.embedding_class must be a string or null, "
                f"got {type(val).__name__}"
            )
        cfg.embedding_class = val or None

    if "hot_list_n" in raw:
        val = raw["hot_list_n"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise ValueError(
                "action_retrieval.hot_list_n must be an integer, "
                f"got {type(val).__name__}"
            )
        if val < 0:
            raise ValueError(
                f"action_retrieval.hot_list_n must be >= 0, got {val}"
            )
        cfg.hot_list_n = val

    if "mode" in raw:
        val = raw["mode"]
        if not isinstance(val, str):
            raise ValueError(
                f"action_retrieval.mode must be a string, got {type(val).__name__}"
            )
        cfg.mode = val

    if "hot_list_seed" in raw:
        val = raw["hot_list_seed"]
        if val == "default":
            cfg.hot_list_seed = "default"
        elif isinstance(val, list):
            for item in val:
                if not isinstance(item, str):
                    raise ValueError(
                        "action_retrieval.hot_list_seed list items must be "
                        f"strings, got {type(item).__name__}"
                    )
            cfg.hot_list_seed = list(val)
        else:
            raise ValueError(
                "action_retrieval.hot_list_seed must be \"default\" or a "
                f"list of strings, got {type(val).__name__!r}"
            )

    return cfg


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
    mcp_search_threshold: int = 30
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
        elif key == "safety" and isinstance(val, dict):
            existing = result.get("safety", {})
            if not isinstance(existing, dict):
                existing = {}
            merged_safety = dict(existing)
            for sub_key, sub_val in val.items():
                if sub_key in ("loop", "timeout", "on_limit") and isinstance(sub_val, dict):
                    merged_safety[sub_key] = {**existing.get(sub_key, {}), **sub_val}
                else:
                    merged_safety[sub_key] = sub_val
            result["safety"] = merged_safety
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


def _warn_legacy_dot_reyn_config(path: Path) -> None:
    """Emit a migration warning if a deprecated <project>/.reyn/config.yaml exists.

    ADR-0031 removed this layer from the 3-layer cascade.  The file is
    intentionally NOT loaded — only a warning is emitted so the user can
    migrate the settings to reyn.local.yaml manually.
    """
    if path.exists():
        import sys
        print(
            f"reyn: warning: {path} is deprecated (ADR-0031 — 3-layer config cascade). "
            "Settings in this file are no longer loaded. "
            "Migrate to reyn.local.yaml, then delete this file.",
            file=sys.stderr,
        )


def _parse_mcp_search_threshold(raw_mcp: object) -> int:
    """Extract ``mcp.search_threshold`` from the raw ``mcp:`` section dict.

    Returns the default (30) when the section is absent, the key is missing,
    or the value is invalid. Accepts 0 (= disable the search tool switch).
    """
    _default = 30  # mirrors ReynConfig.mcp_search_threshold default
    if not isinstance(raw_mcp, dict):
        return _default
    threshold_raw = raw_mcp.get("search_threshold", _default)
    try:
        threshold = int(threshold_raw)
        if threshold < 0:
            threshold = 0
        return threshold
    except (TypeError, ValueError):
        return _default


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
                    "mcp": {}}

    # User global
    user_global = _load_yaml(Path.home() / ".reyn" / "config.yaml")
    merged = _merge(merged, user_global)

    # Project + local
    project_root = _find_project_root(cwd)
    if project_root:
        project = _load_yaml(project_root / "reyn.yaml")
        merged = _merge(merged, project)
        project_local = _load_yaml(project_root / "reyn.local.yaml")
        merged = _merge(merged, project_local)

        # Issue #470: dynamic MCP registry separated from static config.
        # ``.reyn/mcp.yaml`` carries op-managed server entries; merged
        # LAST so it overrides any operator-edited ``mcp.servers`` in
        # reyn.yaml / reyn.local.yaml (= newer installs win, but
        # legacy entries continue to load for backward compat).
        # Shape: ``{"mcp": {"servers": {<name>: {<entry>}}}}`` — same
        # as the section in reyn.yaml, so ``_merge`` handles it
        # without special-casing.
        dynamic_mcp = _load_yaml(project_root / ".reyn" / "mcp.yaml")
        merged = _merge(merged, dynamic_mcp)

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

    safety_raw = merged.get("safety") if isinstance(merged.get("safety"), dict) else {}
    safety = _build_safety_config(safety_raw)
    cost = _build_cost_config(merged.get("cost"))
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
        mcp=dict(merged.get("mcp") or {}),
        mcp_search_threshold=_parse_mcp_search_threshold(merged.get("mcp")),
        python=_build_python_config(merged.get("python")),
        agent=_build_agent_config(merged.get("agent")),
        auth=_build_auth_config(merged.get("auth")),
        chat=_build_chat_config(merged.get("chat")),
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
        web=_build_web_config(merged.get("web")),
        multimodal=_build_multimodal_config(merged.get("multimodal")),
        skill_search=_build_skill_search_config(merged.get("skill_search")),
        plan=_build_plan_config(merged.get("plan")),
        eval=_build_eval_config(merged.get("eval")),
        sandbox=_build_sandbox_config(merged.get("sandbox")),
        self_improvement=_build_self_improvement_config(merged.get("self_improvement")),
        action_retrieval=_build_action_retrieval_config(merged.get("action_retrieval")),
        cron=_build_cron_config(merged.get("cron")),
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


def _build_plan_config(raw: object) -> PlanConfig:
    """Parse ``plan:`` block; unknown keys are ignored (forward-compat)."""
    defaults = PlanConfig()
    if not isinstance(raw, dict):
        return defaults
    step_max_raw = raw.get("step_max_iterations")
    try:
        step_max = int(step_max_raw) if step_max_raw is not None else defaults.step_max_iterations
    except (TypeError, ValueError):
        step_max = defaults.step_max_iterations
    if step_max < 1:
        step_max = defaults.step_max_iterations
    retry_limit_raw = raw.get("retry_limit")
    try:
        retry_limit = int(retry_limit_raw) if retry_limit_raw is not None else defaults.retry_limit
    except (TypeError, ValueError):
        retry_limit = defaults.retry_limit
    if retry_limit < 0:
        retry_limit = defaults.retry_limit
    return PlanConfig(step_max_iterations=step_max, retry_limit=retry_limit)


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
    return CostConfig(
        per_agent_tokens=_build_cost_limit(raw.get("per_agent_tokens")),
        per_agent_cost_usd=_build_cost_limit(raw.get("per_agent_cost_usd")),
        rate_limit_per_minute=rate,
        rate_limit_warn_ratio=warn_ratio,
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
    on_limit_raw = raw.get("on_limit") or {}
    if not isinstance(on_limit_raw, dict):
        on_limit_raw = {}

    loop_defaults = LoopConfig()
    timeout_defaults = TimeoutConfig()

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
        skill_calls_per_chain=_build_cost_limit(
            loop_raw.get("skill_calls_per_chain")
        ),
        skill_tokens_per_chain=_build_cost_limit(
            loop_raw.get("skill_tokens_per_chain")
        ),
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
    on_limit_defaults = OnLimitConfig()
    mode_raw = str(on_limit_raw.get("mode", on_limit_defaults.mode))
    if mode_raw not in ON_LIMIT_MODES:
        import logging
        logging.getLogger(__name__).warning(
            "safety.on_limit.mode=%r is not one of %s; using %r",
            mode_raw, ON_LIMIT_MODES, on_limit_defaults.mode,
        )
        mode_raw = on_limit_defaults.mode
    auto_extend_times_raw = on_limit_raw.get(
        "auto_extend_times", on_limit_defaults.auto_extend_times,
    )
    try:
        auto_extend_times = int(auto_extend_times_raw)
        if auto_extend_times < 0:
            auto_extend_times = on_limit_defaults.auto_extend_times
    except (TypeError, ValueError):
        auto_extend_times = on_limit_defaults.auto_extend_times
    ask_timeout_seconds_raw = on_limit_raw.get(
        "ask_timeout_seconds", on_limit_defaults.ask_timeout_seconds,
    )
    try:
        ask_timeout_seconds = float(ask_timeout_seconds_raw)
        if ask_timeout_seconds < 0:
            ask_timeout_seconds = on_limit_defaults.ask_timeout_seconds
    except (TypeError, ValueError):
        ask_timeout_seconds = on_limit_defaults.ask_timeout_seconds
    on_limit = OnLimitConfig(
        mode=mode_raw,  # type: ignore[arg-type]
        auto_extend_times=auto_extend_times,
        ask_timeout_seconds=ask_timeout_seconds,
    )
    return SafetyConfig(loop=loop, timeout=timeout, on_limit=on_limit)


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
