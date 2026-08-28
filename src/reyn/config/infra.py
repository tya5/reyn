"""reyn.config.infra — infra config: AgentId/Auth/Sandbox/AuditEvents/Eval/Cron/Python. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Literal

from reyn.security.secrets.oauth import OAuthProviderConfig


def _default_agent_id() -> str:
    """Compute the default ``agent_id`` used when reyn.yaml's ``agent_id:`` is
    unset.

    Format: ``reyn/<hostname>``. Pure function so the default is
    inspectable / overridable in tests via the same call site.
    """
    return f"reyn/{socket.gethostname()}"


def _build_agent_id(raw: object) -> str:
    """Parse the top-level ``agent_id:`` scalar from reyn.yaml.

    #4174 T5: ``agent:`` (a namespace wrapping a single field, ``id``) is
    flattened to a plain top-level scalar — same disposition as T1's
    ``python:`` (a single-field wrapper that added indirection without
    adding structure; #4174's own diagnosis of the OLD shape was "the
    substance is only `id`", so keeping a nested block after the rename
    would leave that exact redundancy in a new name, `agent_id.id`).
    Registered in ``config_schema._RENAMED_CONFIG_KEYS`` with
    ``destination=None`` (a value TRANSFORM — dict to scalar — not a
    plain rename `reyn config migrate` can auto-rewrite; reported for
    manual review instead, same category as
    ``_RENAMED_SANDBOX_POLICY_KEYS``'s boolean-inversion entries).

    ``None`` / missing key / empty string → default (``reyn/<hostname>``),
    matching the old ``agent.id`` blank-falls-back-to-default behavior so
    an operator who migrates by hand and leaves the value blank does not
    end up with an empty ``agent_id`` leaking into events / headers.
    """
    if raw is None or raw == "":
        return _default_agent_id()
    if not isinstance(raw, str):
        raise ValueError(f"agent_id must be a string, got {type(raw).__name__}")
    return raw


@dataclass
class DelegationConfig:
    """``delegation:`` — cross-agent delegation policy (#2081).

    ``capability_default`` selects the capability floor a DELEGATED agent (one
    spawned by another agent's delegation, recursively) receives when it is
    otherwise unbound by a topology ``capability_profile``:

    - ``inherit`` (default) — a delegate inherits the spawner's capability
      surface; no extra narrowing. Byte-identical to pre-#2081.
    - ``deny`` — an unbound delegate is narrowed by the built-in restrictive
      ``_delegate`` profile (the dangerous-tool classes denied: re-delegation /
      side-effect-exec / memory-write / MCP-install) unless a topology
      capability_profile binding re-grants it (the binding REPLACES the default,
      since composition is most-restrictive-wins and cannot re-grant). The
      default-deny propagates RECURSIVELY — a sub-delegate is itself a delegate,
      so a re-granted coordinator's own sub-delegates are still default-denied
      (no laundering).

    Only the unbound-delegate fallback is affected; a top-level agent and any
    topology-bound agent are unchanged.
    """

    capability_default: str = "inherit"

    def __post_init__(self) -> None:
        if self.capability_default not in ("inherit", "deny"):
            raise ValueError(
                "delegation.capability_default must be 'inherit' or 'deny', "
                f"got {self.capability_default!r}"
            )


def _build_delegation_config(raw: object) -> DelegationConfig:
    """Parse ``delegation:`` from reyn.yaml (#2081).

    ``None`` / missing block / empty dict → default (= ``inherit``, byte-identical
    to pre-#2081). The value is validated by ``DelegationConfig.__post_init__``.
    """
    if raw is None:
        return DelegationConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"delegation must be a mapping, got {type(raw).__name__}"
        )
    cap = raw.get("capability_default")
    if cap is None:
        return DelegationConfig()
    if not isinstance(cap, str):
        raise ValueError(
            "delegation.capability_default must be a string, "
            f"got {type(cap).__name__}"
        )
    return DelegationConfig(capability_default=cap)


@dataclass
class RouterConfig:
    """``llm.router:`` — litellm.Router resilience config (#1829).

    The Router gives provider-resilience (infra-exception retry with native
    Retry-After respect, per-deployment cooldown, cross-model fallback chain)
    without Reyn re-implementing it. **Default OFF** — ``use=False`` keeps the
    direct ``litellm.acompletion`` path (byte-identical to pre-#1829).

    Single config surface: ``use`` / ``num_retries`` supersede the legacy
    ``REYN_LLM_USE_ROUTER`` / ``REYN_LLM_ROUTER_NUM_RETRIES`` env vars, which
    remain a back-compat fallback when no reyn.yaml router config is loaded
    (the ``ssl_verify`` → env → default idiom).
    """

    use: bool = False
    num_retries: int = 3
    # model_name → [fallback model_names]. Converted to litellm's
    # ``[{primary: [fallbacks]}]`` form when the Router is built. Empty → no
    # chain (single-deployment Router).
    fallbacks: dict = field(default_factory=dict)
    # seconds a deployment is cooled down after ``allowed_fails`` failures
    # (None → litellm default). Only meaningful with a fallback chain.
    cooldown_time: float | None = None
    # failures before a deployment is cooled down (None → litellm default).
    allowed_fails: int | None = None
    # per-exception-type retry counts (None → litellm defaults, i.e. no typed
    # policy). A mapping of RetryPolicy field names → counts; constructed into a
    # ``litellm.RetryPolicy`` at Router build time. Supported keys:
    #   RateLimitErrorRetries, TimeoutErrorRetries, BadRequestErrorRetries,
    #   AuthenticationErrorRetries, ContentPolicyViolationErrorRetries,
    #   InternalServerErrorRetries.
    retry_policy: dict | None = None


@dataclass
class RetryConfig:
    """``llm.retry:`` — backoff timing for the Reyn self-retry layer (#1835).

    Controls TIMING only; semantic-retry behaviours (EmptyLLMResponseError,
    empty_stop_retry, compaction shrink) are unaffected.

    ``jitter=true`` (default): equal jitter (AWS pattern) —
      ``sleep = backoff/2 + uniform(0, backoff/2)``
      where ``backoff = min(base * 2**attempt, max_backoff)``.
      Prevents thundering herd when parallel chains retry in lockstep.

    ``respect_retry_after=true`` (default): when a retryable exception carries
      a ``Retry-After`` header (delta-seconds or HTTP-date), honour it (capped
      at ``_LLM_RETRY_MAX_BACKOFF_S``) instead of the computed jittered backoff.
      Lets the provider's guidance drive wait time on 429/503 responses.
    """

    jitter: bool = True
    respect_retry_after: bool = True


@dataclass
class LLMConfig:
    """``llm:`` — LLM-layer config (#1829 router, #1835 retry, #4174 T3 the
    model-selection domain).

    #4174 T3: ``model`` / ``models`` / ``model_class_by_purpose`` / ``api_base``
    / ``prompt_cache_enabled`` moved here from top-level ``ReynConfig`` fields —
    they were always LLM-domain settings scattered at the top level while
    ``llm.router`` / ``llm.retry`` (also LLM-domain) already lived in their own
    block. Plain rename, same shapes, registered in
    ``config_schema._RENAMED_CONFIG_KEYS`` so ``reyn config migrate`` rewrites
    old top-level keys automatically.
    """

    router: RouterConfig = field(default_factory=RouterConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    # #1672 default model class used when a phase has no model_class.
    model: str = field(
        default="standard",
        metadata={"desc": "Default model class used when a phase has no model_class."},
    )
    # Map of model class names to LiteLLM model strings.
    models: dict[str, "str | dict"] = field(
        default_factory=dict,
        metadata={"desc": "Map of model class names to LiteLLM model strings."},
    )
    # #1672 per-purpose model-class override (router / control_ir / tool / judge).
    # Unset purpose -> `model` (see MODEL_CLASS_PURPOSES / ReynConfig.model_class_for).
    model_class_by_purpose: dict[str, str] = field(default_factory=dict)
    # #4206 T1 (②bounding, ``model`` key): operator-declared CEILING on the
    # model class a call may use — restrict-only, reject-not-clamp (same
    # shape as #3903①'s ``SandboxPolicy.max_timeout_seconds``). ``None``
    # (default) means unbounded, byte-identical to before this field existed.
    # Enforced at ``recorded_acompletion`` (#1190 chokepoint), never at a
    # call site, so a future call site cannot forget it.
    model_max_class: "str | None" = field(
        default=None,
        metadata={"desc": "Ceiling on the model class calls may use (light/standard/strong). Unset = unbounded."},
    )
    # LiteLLM proxy: non-secret base URL only. API keys must be env vars
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) — never stored in config files.
    api_base: str = field(
        default="",
        metadata={"desc": "LiteLLM proxy base URL. Set this if you route requests through a local proxy."},
    )
    # Attach Anthropic-style cache_control markers to the system prompt so
    # providers that support prompt caching (Anthropic, AWS Bedrock Claude) can
    # reuse the prefix across calls. Ignored by providers that don't recognize
    # cache_control (Gemini / OpenAI proxies pass-through).
    prompt_cache_enabled: bool = True


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
    """#1672: parse ``llm.model_class_by_purpose`` (purpose → model class).
    Unknown purpose keys WARN (not error) — a typo would silently never apply,
    so flag it decision-enablingly while staying forward-compatible with future
    purposes.

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
                "llm.model_class_by_purpose.compaction is no longer "
                "configurable (#3785) — compaction always follows the "
                "conversation's active model now (it did not track a "
                "`/model` switch before, which this removal fixes). Remove "
                "this key from your reyn.yaml/reyn.local.yaml."
            )
        if key not in MODEL_CLASS_PURPOSES:
            import logging
            logging.getLogger(__name__).warning(
                "llm.model_class_by_purpose.%s is not a known purpose %s — "
                "it will never be applied; check for a typo.",
                key, sorted(MODEL_CLASS_PURPOSES),
            )
        out[key] = str(v)
    return out


def _build_model_max_class(raw: object) -> "str | None":
    """#4206 T1: parse ``llm.model_max_class`` — a ceiling, so (unlike
    ``model_class_by_purpose``'s per-key warn-only tolerance) an unrecognized
    value is a hard fail-fast, not a silent pass-through: a ceiling that
    silently doesn't apply is a widened bound, not a typo you can shrug off.
    """
    if raw is None:
        return None
    from reyn.llm.model_resolver import STANDARD_CLASSES

    value = str(raw)
    if value not in STANDARD_CLASSES:
        raise ValueError(
            f"llm.model_max_class must be one of {list(STANDARD_CLASSES)}, "
            f"got {value!r}"
        )
    return value


def _build_retry_config(raw: object) -> RetryConfig:
    """Parse ``llm.retry:`` from reyn.yaml. None/missing → defaults (both true)."""
    if raw is None:
        return RetryConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"llm.retry must be a mapping, got {type(raw).__name__}")
    d = RetryConfig()
    return RetryConfig(
        jitter=bool(raw.get("jitter", d.jitter)),
        respect_retry_after=bool(raw.get("respect_retry_after", d.respect_retry_after)),
    )


def _build_router_config(raw: object) -> RouterConfig:
    """Parse ``llm.router:`` from reyn.yaml. None/missing → defaults (router OFF)."""
    if raw is None:
        return RouterConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"llm.router must be a mapping, got {type(raw).__name__}")
    d = RouterConfig()
    fb = raw.get("fallbacks", d.fallbacks)
    if fb and not isinstance(fb, dict):
        raise ValueError(
            "llm.router.fallbacks must be a mapping (model → [fallbacks]), "
            f"got {type(fb).__name__}"
        )
    rp_raw = raw.get("retry_policy")
    if rp_raw is not None and not isinstance(rp_raw, dict):
        raise ValueError(
            f"llm.router.retry_policy must be a mapping, got {type(rp_raw).__name__}"
        )
    return RouterConfig(
        use=bool(raw.get("use", d.use)),
        num_retries=int(raw.get("num_retries", d.num_retries)),
        fallbacks={
            str(k): [str(x) for x in (v or [])] for k, v in (fb or {}).items()
        },
        cooldown_time=(
            float(raw["cooldown_time"]) if raw.get("cooldown_time") is not None else None
        ),
        allowed_fails=(
            int(raw["allowed_fails"]) if raw.get("allowed_fails") is not None else None
        ),
        # None → absent (default litellm behavior); mapping → constructed at Router
        # build time into a litellm.RetryPolicy object.
        retry_policy={str(k): int(v) for k, v in rp_raw.items()} if rp_raw else None,
    )


def _build_llm_config(raw: object) -> LLMConfig:
    """Parse ``llm:`` from reyn.yaml. None/missing → defaults.

    #4174 T3: also parses ``model`` / ``models`` / ``model_class_by_purpose`` /
    ``api_base`` / ``prompt_cache_enabled`` — moved here from top-level
    ``ReynConfig`` fields (see ``LLMConfig``'s own docstring)."""
    if raw is None:
        return LLMConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"llm must be a mapping, got {type(raw).__name__}")
    _models_raw = raw.get("models")
    if _models_raw is not None and not isinstance(_models_raw, dict):
        import logging
        logging.getLogger(__name__).warning(
            "llm.models must be a mapping; got %s — ignoring it.",
            type(_models_raw).__name__,
        )
        _models_raw = {}
    return LLMConfig(
        router=_build_router_config(raw.get("router")),
        retry=_build_retry_config(raw.get("retry")),
        model=str(raw.get("model", "standard")),
        models={
            str(k): (v if isinstance(v, dict) else str(v))
            for k, v in (_models_raw or {}).items()
        },
        model_class_by_purpose=_build_model_class_by_purpose(
            raw.get("model_class_by_purpose"),
        ),
        model_max_class=_build_model_max_class(raw.get("model_max_class")),
        api_base=str(raw.get("api_base") or ""),
        prompt_cache_enabled=bool(raw.get("prompt_cache_enabled", True)),
    )


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
class AuditEventsConfig:
    """`audit_events:` — audit log rotation + automatic purge policy (PR20, #4479).

    #4174 T5: renamed from `events:` / `EventsConfig` — bare "event" is the
    exact shape CLAUDE.md's cross-cutting-band note bans ("event" is three
    distinct things: audit-event / WAL-event / hook-event; this class was
    always audit-event only, never any of the other two).

    Chat session events are appended to a folder under
    `.reyn/events/agents/<name>/chat/<YYYY-MM>/` and rotated when either
    the active file's size exceeds `max_bytes` OR its age (or local date)
    exceeds `max_age_seconds`. Setting both to 0 disables rotation, which
    is the single-run mode (1 run = 1 file).

    **Automatic purge (#4479, owner ruling 2026-08-13)** — two independent
    axes, EITHER firing deletes a file (owner: "日数orサイズ"; `and` would
    mean disabling one axis silently disables the other too):

    - ``cleanup_period_days`` (default 30): files whose filename start-date
      is older than `today - cleanup_period_days` are purge targets.
      **This is a borrowed CONVENTION, not a measurement** — no local-CLI
      precedent measures `.reyn/events`'s own growth rate; 30 borrows the
      nearest comparable tool's own default (Claude Code's own
      `cleanupPeriodDays`, another local-agent CLI).
    - ``max_disk_usage_percent`` (default 10): once the events directory's
      own total size exceeds this percent of the filesystem's CURRENT free
      space, oldest files are purged until back under. **Relative, not
      absolute** — an absolute byte ceiling would need reyn's own measured
      growth rate to set safely (unmeasured); a relative ceiling doesn't.
      10 borrows systemd-journald's own `SystemMaxUse` convention for the
      identical reason — again borrowed, not derived from reyn's own data.

    **`0` on either axis disables that axis** (deliberately, an explicit
    reversal of THIS class's earlier stance on `cleanup_period_days`, which
    used to REJECT `0` as a footgun — see below). Both axes documented
    here plainly rather than left ambiguous: Claude Code carries an open
    report of its own `cleanupPeriodDays` knob being unclear about what
    `0` means, and this is that precedent deliberately not repeated.

    Purge runs automatically (fire-and-forget, off the event loop) at
    session start and at rotation — see `EventStore`/`core/events/
    event_purge.py` for the trigger + the actual selection/deletion logic,
    which `reyn events purge` (the CLI command) also calls, so there is
    exactly one place that decides "which files are purge targets."

    #4479 drift note: this docstring previously said `cleanup_period_days`
    defaulted to `None` (disabled) and rejected `0`. Both are now the
    OPPOSITE of what this field does — kept only as a historical note in
    case an old comment elsewhere still cites the rejected-0 behavior.
    """
    max_bytes: int = 10 * 1024 * 1024     # 10 MB
    max_age_seconds: int = 24 * 60 * 60   # 1 day
    cleanup_period_days: int = 30
    max_disk_usage_percent: float = 10.0
    # #4496 PR-2: the WRITE-side backend. `local` (default) preserves
    # current behavior unchanged — audit-events land under `.reyn/events`
    # exactly as before this field existed. `discard` writes nothing
    # (sink-null); subscriber delivery (CUI/AG-UI, hooks, OTEL) and the
    # per-emitter `audit_seq` continuity are UNCHANGED either way — see
    # `reyn.core.events.backend`'s module docstring for the structural
    # guarantee. `network` is NOT yet a valid value — its on-failure
    # semantics (discard-and-let-seq-show-it / local spool / halt-the-run)
    # are still an open owner decision (#4496); an operator who sets it
    # (or any other string) gets the standard unknown-VALUE-falls-back-
    # to-default tolerance — see the parser below. `Literal[...]` (not a
    # bare `str`, per lead-coder review) matches this repo's convention
    # for a closed, small value set (`retry_backoff` / `chat.mode` /
    # `render_mode` / `on_oversize` all use it) — `tool_use.scheme` /
    # `.transport` stay plain `str` because THEIR domain is an open,
    # pluggable registry a literal type can't enumerate; this field's
    # domain is closed, so it belongs on the Literal side of that split.
    backend: Literal["local", "discard"] = "local"
    # #4960 (architect ruling C): ``agent_delta`` (one audit-event per
    # streamed content chunk) is coalesced to one durable write-side
    # record per this many fragments, or `agent_delta_coalesce_interval_
    # ms` milliseconds, whichever comes first — plus one final record on
    # stream end. See `LocalEventBackend`'s own module-level constants
    # (`core/events/backend.py`) for the measured rationale (2000-delta
    # real-run benchmark) these defaults are derived from. Live TUI/AG-UI
    # delivery is completely unaffected — this only throttles what
    # reaches disk.
    agent_delta_coalesce_fragments: int = 100
    agent_delta_coalesce_interval_ms: int = 2_000
    # #4666 (owner ruling): the streamed reply's own CONTENT
    # (`agent_delta`'s `text` field) is opt-in, default off, its OWN
    # knob — deliberately NOT unified with any other content opt-in
    # (owner: "agent_delta は opt-in。完成の会話も opt-in。別々の config
    # ノブにして" — one toggle must never cover both). Mirrors the
    # OpenTelemetry GenAI semantic convention this issue follows: "every
    # attribute that can hold prompt or output content is Opt-In ...
    # default is metadata-only" (routinely-PII rationale). Coalescing
    # itself (#4960, the fields ABOVE this one) is untouched by this flag
    # — only the `text` field within an already-coalesced record is
    # conditional; `chain_id`/`round_index`/`coalesced_fragment_count`
    # are always kept, so #4960's own reason for existing (cost
    # accountability for a call whose usage record never lands) survives
    # `text` being dropped.
    agent_delta_include_text: bool = False
    # #4666②: the completed model→user text — the terminal reply, any
    # force-close/wrap-up text, tool_calls-round accompanying text (all via
    # `agent_response_committed`), and the `ask_user` question (via the
    # pre-existing `user_intervention_requested`, see that emit site's own
    # comment) — is opt-in, default off, its OWN knob (owner ruling, same
    # verbatim instruction as `agent_delta_include_text` above: "別々の
    # config ノブにして", one toggle must never cover both). Same shape as
    # `agent_delta_include_text`: the event(s) fire unconditionally either
    # way (their existence, and every other field, is unconditional
    # audit-trail evidence — "a response was committed" / "a question was
    # asked" survives this flag being off) — `LocalEventBackend.write()`
    # drops ONLY the free-text field(s) (`text` / `question`+`suggestions`
    # +`options`) from the DURABLE record while this is off. Live
    # TUI/AG-UI subscriber delivery, and any opt-in OTEL subscriber, are
    # UNAFFECTED by this flag — same disclosure as `agent_delta_include_
    # text`'s own comment: this only throttles what reaches disk.
    completed_response_include_text: bool = False
    # #4666 item ③ (owner ruling): "user input" gets its OWN opt-in too —
    # deliberately separate from `agent_delta_include_text` AND
    # `completed_response_include_text` above (owner: each content opt-in
    # gets its own knob, never a shared toggle). Covers 6 audit-event
    # kinds carrying a user's own typed/chosen text — see
    # `LocalEventBackend`'s `_USER_INPUT_CONTENT_FIELDS` (core/events/
    # backend.py) for the exact kind -> field mapping and the AST census
    # behind the count. Default off, same metadata-only-by-default
    # rationale as `agent_delta_include_text`. ⚠️ Known gap this flag does
    # NOT close: ask_user's question/answer also reach the audit log via
    # `tool_called.args`/`tool_returned.result` (a separate emit path) —
    # see the same docstring.
    user_input_include_text: bool = False
    # #4975 (architect ruling, issuecomment-5384508845, correcting an
    # earlier "messages" left-operand that named a knob which does not
    # exist): a provider's 4xx/5xx error response can echo BACK content
    # from the request it rejected — ``llm_request_error``'s
    # ``provider_body``/``provider_response`` fields (``llm.py``'s
    # ``_emit_llm_request_error``). reyn does not choose the shape of a
    # provider's own error body, so it cannot tell in advance which of
    # the 3 content classes above (streamed reply text, completed
    # response text, user-typed text) a given provider might quote back —
    # any of the 3 could be the one that leaks. This is therefore its OWN
    # separate opt-in (never unified with any single one of the 3 above —
    # "the same wall, one more narrow door inside it"), gated by a
    # LATTICE-MEET (mirrors ``resolved_profile_for``'s own idiom,
    # ``compose_resolved is a lattice-meet (∩ allow, ∪ deny)``): showing
    # ``provider_body``/``provider_response`` requires ALL 3 content
    # knobs above to ALSO be on, AND this flag itself — the narrowest
    # participant wins. An operator who has opted out of even ONE content
    # class must not be handed a blob that could contain it; an operator
    # who opted into all 3 has not thereby also opted into "reyn shows me
    # whatever an external provider chose to quote" (a genuinely
    # different, provider-controlled risk), hence this flag being its own
    # opt-in on top of the meet, never implied by the other 3 alone.
    # Default off, same metadata-only-by-default rationale as the 3
    # above. OFF does not mean silent: ``error_type``/``status_code`` are
    # unconditional (unchanged), and ``provider_body_length`` /
    # ``provider_response_length`` are added so "a body existed but was
    # not shown" stays distinguishable from "there was none" — see
    # ``_emit_llm_request_error``'s own comment for exactly which fields
    # this flag gates.
    provider_body_include_text: bool = False
    # #4975: reyn cannot bound a provider's own error-body size — an
    # explicit, operator-adjustable cap (never a baseless embedded
    # constant, CLAUDE.md's own "no unjustified constants" rule) applied
    # ONLY when ``provider_body_include_text`` (and the meet) is on;
    # ``provider_body_length`` above is unaffected (always the TRUE
    # length, truncation-independent, so a truncated body is still
    # honestly labeled).
    provider_body_max_chars: int = 4000


@dataclass
class ArtifactsConfig:
    """``artifacts:`` — the artifact-ref table fallback's own row cap
    (#4601, lead-coder/architect ruling).

    ``list_refs_for_agent`` (``data/workspace/artifact_ref.py``) is the
    ONE fallback join point both the AG-UI endpoint's ``artifact_list_request``
    handler and ``InProcessTransport``'s own local read share (#4494 design
    C) — the table is append-only and now persist-tier (#4584), so both
    call sites read an UNBOUNDED, ever-growing list with no cap of their
    own. This is a single-knob fix at that one join point, not two
    independent caller-side caps (architect's own #4601 finding: capping
    only the endpoint leaves the TUI's identical fallback path broken).

    **``remote_fallback_limit`` is a UX-scale cap, not a performance
    one** (architect's #4601 measurement-by-order-of-magnitude, not a
    live benchmark): a single ``stat()`` costs order-microseconds, so
    even 10,000 rows costs tens of milliseconds — the constraint that
    actually binds is how many newest-first rows a human operator would
    ever scroll through in a list pane, which is a couple of dozen at
    most. The default below is therefore a UX default, not a derived
    number — which is exactly why it MUST be operator-adjustable (owner's
    standing instruction: never embed a baseless number with no
    user-facing way to change it) rather than defended with a rationale
    comment no fixed number could honestly carry (the "correct" N
    genuinely depends on the pane's own height/usage, which this config
    layer cannot see).

    The list is newest-first (``list_refs_for_agent``'s own convention,
    matching ``collect_artifact_rows``), so ``entries[:remote_fallback_limit]``
    is always the N MOST RECENT artifacts, never an arbitrary slice.
    Truncation is disclosed, never silent — see ``chrome.py``'s
    consolidated fallback-source disclosure text ("newest N of M")."""

    remote_fallback_limit: int = 50


@dataclass
class StorageConfig:
    """``storage:`` — the PROJECT-wide (cross-session) disk-usage cap for
    ``.reyn/memory/history-content/`` (#5366, architect ruling,
    issuecomment-5451389251).

    #5366 measured (e2e-coder) that ``MediaStoreConfig`` — and therefore
    its own ``history_content_max_bytes`` field — is constructed FRESH
    per ``Session`` (``session.py:1061``), i.e. structurally per-store.
    The owner's own ruling for #5366 ("リソース上限はユーザが設定するこ
    とになる" — the cap the user can actually NAME is one number for
    ``.reyn/`` as a whole, not "N bytes per session", which silently
    means "N × (however many sessions exist) total" since the user does
    not choose how many sessions get spawned) has no existing home: no
    top-level ``ReynConfig`` section carried a project-wide storage
    concept before this one.

    ``max_bytes`` is DELIBERATELY not named ``history_content_max_bytes``
    — that name already exists on ``MediaStoreConfig`` as the per-store
    fail-safe #5388's own per-session eviction still uses (kept, per
    architect's ruling: "同じ名前の2つの上限は必ず混同されます" — reusing
    the name would make it unreadable which of the two numbers is
    actually in effect for a given eviction). The two are independent:
    this field bounds the WHOLE project's history-content tree across
    every session; the per-store field remains a backstop bounding one
    session's own directory only.

    ``None`` (the default) means unlimited/off — no separate boolean:
    architect's ruling ("別の真偽値を作らない — 2つの表現が食い違いま
    す") that a second on/off flag alongside a numeric field is a
    guaranteed-to-diverge redundant representation.

    ``pin`` names agent names (not session ids — an agent's OWN identity
    is the unit an operator can actually declare in reyn.yaml; per-session
    pinning would need to name a session id an operator does not choose)
    whose history-content is NEVER an eviction candidate for this
    project-wide cap, regardless of process liveness."""

    max_bytes: "int | None" = None
    pin: "list[str]" = field(default_factory=list)


_SANDBOX_BACKENDS = {"auto", "seatbelt", "landlock", "noop"}
_SANDBOX_ON_UNSUPPORTED = {"warn", "error", "ignore"}
# #3823 ①②: compat / strict — NOT "custom" (owner ruling, 2026-08-09:
# "custom" was never a third DIRECTION, it was the symptom of mode and
# `policy:` not having a defined composition rule; #3823's settled design —
# mode decides only the DEFAULT for an axis the operator left unset, while
# allow_X/deny_X (or the bare bool) are ALWAYS writable regardless of mode —
# removes the need for a third value: "the operator writes both direction
# and content" is just "the operator writes allow_X/deny_X explicitly",
# already true under either compat or strict). NOT "off" either (owner
# ruling: "off" is expressible as compat with every axis's allow_X/deny_X
# left at the compat default, so it does not need its own enum member).
# strict is now WIRED (resolve_sandbox_policy, security/sandbox/policy.py) —
# see that module for the resolution algorithm.
_SANDBOX_MODES = {"compat", "strict"}
DEFAULT_SANDBOX_MODE = "compat"

# #3901 PR-B ④: SandboxPolicy field renames, keyed by the OLD name an operator
# might still write in reyn.yaml. Every entry names the new key AND, for the
# two renames whose value sense inverts, says so explicitly — a rename that
# silently swapped `allow_subprocess: false` for `deny_subprocess: false`
# would keep the config valid while reversing the operator's actual intent,
# which is worse than refusing it outright.
_RENAMED_SANDBOX_POLICY_KEYS: dict[str, str] = {
    "allow_subprocess": (
        "'allow_subprocess' was renamed to 'subprocess' (#3823; it passed "
        "through 'deny_subprocess' in between, #3901) — same positive sense "
        "as this original name: `subprocess: true` = allowed (the default)."
    ),
    # #3823: the #3901-era internal-vocabulary names, now superseded by the
    # config-facing vocabulary a `sandbox.policy:` block actually accepts
    # (SandboxPolicy's OWN field names are unchanged — see
    # security/sandbox/policy.py's _translate_sandbox_policy_config — only
    # what an operator WRITES in reyn.yaml changed).
    "deny_subprocess": (
        "'deny_subprocess' was renamed to 'subprocess' (#3823) — the VALUE "
        "INVERTS BACK: `deny_subprocess: true` is now `subprocess: false`, "
        "and `deny_subprocess: false` (the old default) is now "
        "`subprocess: true` (still the default)."
    ),
    "write_paths": (
        "'write_paths' was renamed to 'allow_write_paths' (#3823) — same "
        "meaning, no value change."
    ),
    "read_deny_paths": (
        "'read_deny_paths' was renamed to 'deny_read_paths' (#3823, "
        "word-order fix — <direction>_<axis>_<unit>) — same meaning, no "
        "value change."
    ),
    "write_deny_paths": (
        "'write_deny_paths' was renamed to 'deny_write_paths' (#3823, "
        "word-order fix) — same meaning, no value change."
    ),
    "env_deny_names": (
        "'env_deny_names' was renamed to 'deny_env_names' (#3823, "
        "word-order fix) — same meaning, no value change. A new "
        "'allow_env_names' key is also now available (SWITCHES the env axis "
        "to allow-list semantics — see security/sandbox/policy.py's "
        "resolve_passthrough_env)."
    ),
    "env_passthrough": (
        "'env_passthrough' was renamed to 'deny_env_names' (it passed "
        "through 'env_deny_names' in between, #3901) and changed from an "
        "ALLOW-list to a DENY-list (owner ruling: full env compat by "
        "default). `env_passthrough: []` (pass nothing extra) is no longer "
        "expressible as an empty list — the new default (deny_env_names "
        "omitted) passes EVERYTHING; to keep specific names blocked, list "
        "them in deny_env_names instead, or use the new 'allow_env_names' "
        "key for allow-list semantics."
    ),
    "read_paths": (
        "'read_paths' was removed — every sandbox backend already ignored it "
        "(the #1199 broad-read realignment made reads unconditional); there "
        "is no replacement field."
    ),
}


def _sandbox_policy_freeform_validator(
    config_policy: dict,
) -> "dict[str, object]":
    """#4174 T0: ``sandbox.policy``'s own registered
    :func:`~reyn.config.config_schema.register_freeform_leaf_validator` —
    the ONE place ``sandbox.policy`` plugs its inner vocabulary
    (``security.sandbox.policy._SANDBOX_POLICY_CONFIG_KEYS``) into the
    shared unknown-key walk, so it stops being a hand-maintained special
    case (#3823's original ``SandboxConfig.__post_init__`` raise, now
    removed — see that method). Reuses the existing rich per-key rename
    guidance in :data:`_RENAMED_SANDBOX_POLICY_KEYS` as the
    :class:`~reyn.config.config_schema.RenamedKeyHint` note rather than
    discarding it — an operator on a pre-#3823 config still gets told
    exactly where a key moved (and, where it applies, that its value sense
    inverted), not just "unknown".

    Every hint here sets ``destination=None`` (lead-coder's block on
    #4190) — a sandbox.policy rename's guidance describes a per-key VALUE
    TRANSFORM (e.g. a boolean inversion), not a plain relocation, so
    ``reyn config migrate`` must never auto-rewrite it; the operator fixes
    it by hand per the shown note.

    Defined here (the config layer, which already imports from
    ``security.sandbox.policy``) rather than in ``config_schema.py``
    itself, which must NOT import a leaf module — see
    ``register_freeform_leaf_validator``'s own docstring for why.
    """
    from reyn.config.config_schema import RenamedKeyHint
    from reyn.security.sandbox.policy import unknown_sandbox_policy_config_keys

    result: "dict[str, object]" = {}
    for key in unknown_sandbox_policy_config_keys(config_policy):
        note = _RENAMED_SANDBOX_POLICY_KEYS.get(key)
        result[key] = RenamedKeyHint(note=note) if note is not None else None
    return result


def _register_sandbox_policy_validator() -> None:
    """Register :func:`_sandbox_policy_freeform_validator` onto
    ``config_schema`` at import time — called once, below, at module load."""
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator(
        "sandbox.policy", _sandbox_policy_freeform_validator
    )


_register_sandbox_policy_validator()


# ---------------------------------------------------------------------------
# #4655: every OTHER ``is_dict_leaf`` field in the ``ReynConfig`` schema gets
# an EXPLICIT disposition — Kind ① (a real inner-vocabulary validator, same
# ``register_freeform_leaf_validator`` mechanism as ``sandbox.policy`` above)
# or Kind ② (``register_freeform_leaf_open`` — "we looked, it's genuinely
# open, we deliberately do not check it"). Never silence: an unregistered
# leaf and a declared-open leaf both currently accept every sub-key, but
# only the completeness check (``config_schema.unregistered_freeform_leaves``,
# asserted empty by
# ``tests/core/test_4655_freeform_leaf_registration_completeness.py``) can
# tell "nobody looked at this yet" apart from "somebody looked and it's
# open" — that is the whole defect this issue closes (a future 19th
# dict-leaf field silently joining the unregistered pile the way these did).
#
# All registered here, in the config layer (same home as the
# ``sandbox.policy`` precedent above) rather than scattered across each
# leaf's own owning module — each validator does its OWN lazy import of the
# real consumer it verified (matching ``_sandbox_policy_freeform_validator``'s
# own pattern), so this module still never eagerly imports a leaf module,
# only defers the import to validate-time, when an operator's config
# actually has something to check.
# ---------------------------------------------------------------------------


def _external_transports_freeform_validator(
    raw: dict,
) -> "dict[str, object]":
    """#4655 Kind① — ``external_transports``'s real finite vocabulary is
    ONE LEVEL DEEPER than its own direct sub-keys: the direct sub-keys are
    transport NAMES, genuinely operator-chosen (never flagged here). Each
    transport's own entry, however, is read by
    :func:`reyn.runtime.external_routing.parse_external_transports` via
    exactly two literal keys — ``mcp_tool`` / ``args_template`` (see that
    function's own docstring and body) — any other per-entry key is
    silently dropped by the defensive ``.get()`` parse, never applied.

    Returns keys relative to ``external_transports`` itself, two levels
    deep (e.g. ``"broker.foo_bar"``) — :func:`~reyn.config.config_schema.
    unknown_config_keys` prefixes them with ``external_transports.`` the
    same way it prefixes a one-level relative key, so the final report
    reads ``external_transports.broker.foo_bar``.
    """
    result: "dict[str, object]" = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        for sub_key in entry:
            if sub_key not in ("mcp_tool", "args_template"):
                result[f"{name}.{sub_key}"] = None
    return result


def _register_external_transports_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator(
        "external_transports", _external_transports_freeform_validator
    )


_register_external_transports_validator()


#: #4655 — the ONLY two top-level sub-keys ever read from a raw ``mcp:``
#: dict anywhere in the codebase: ``servers`` (``config.mcp.get("servers")``
#: — e.g. ``Session._mcp_servers_flat`` in ``runtime/session.py``,
#: ``interfaces/cli/commands/pipe.py``'s ``configured_mcp_servers``) and
#: ``registries`` (``config/loader.py``'s ``REYN_MCP_REGISTRY_URLS``
#: propagation, consumed by ``mcp/registry.py`` and
#: ``core/registry/client.py``). Not the same check as #4631's
#: ``_mcp_misplaced_server_entries`` (that one flags a key whose VALUE is
#: shaped like a server entry written at the wrong depth — a narrower,
#: shape-based defect); this one flags any OTHER direct sub-key at all.
_MCP_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"servers", "registries"})


def _mcp_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — ``mcp``'s own inner vocabulary: see
    :data:`_MCP_TOP_LEVEL_KEYS`."""
    return {key: None for key in raw if key not in _MCP_TOP_LEVEL_KEYS}


def _register_mcp_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator("mcp", _mcp_freeform_validator)


_register_mcp_validator()


#: #4655 — ``chat.compaction.component_weights``'s real finite vocabulary,
#: matching ``CompactionConfig``'s own docstring (``config/chat.py``) and
#: its ``default_factory`` dict keys: head / body / tail / new_msg /
#: compaction_batch. All five are consumed — the first four by name at
#: ``services/compaction/engine.py``'s ``compute_budgets`` (``cw.get("head"
#: /"body"/"tail"/"new_msg")``); ``compaction_batch`` is never fetched by
#: name there but DOES count toward the normalisation denominator
#: (``sum(cw.values())``) — a deliberate budget-reservation weight, per
#: ``CompactionConfig``'s own docstring, not a dead key.
_COMPONENT_WEIGHT_KEYS: frozenset[str] = frozenset({
    "head", "body", "tail", "new_msg", "compaction_batch",
})


def _component_weights_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — see :data:`_COMPONENT_WEIGHT_KEYS`."""
    return {key: None for key in raw if key not in _COMPONENT_WEIGHT_KEYS}


def _register_component_weights_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator(
        "chat.compaction.component_weights", _component_weights_freeform_validator
    )


_register_component_weights_validator()


def _model_class_by_purpose_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — reuses :data:`MODEL_CLASS_PURPOSES` (the SAME
    frozenset ``_build_model_class_by_purpose`` already checks against at
    real ``load_config()`` time) so ``reyn config validate`` can ALSO see
    an unknown ``llm.model_class_by_purpose`` key — closing the off-surface
    gap noted in that function's own docstring: ``build_policy_tier_config``
    (what ``reyn config validate`` walks) returns the raw merged dict
    without ever calling ``_build_model_class_by_purpose``, so before this
    validator, a typo'd purpose here was invisible to ``validate`` even
    though it already warned at real startup.

    ``compaction`` gets its own explanatory note (mirroring
    ``_build_model_class_by_purpose``'s hard-fail message) rather than a
    bare unknown-key ``None`` — it is a KNOWN, deliberately-removed key
    (#3785), not a typo, and this reporting path must never raise (owner
    ruling — see ``unknown_config_keys``'s own docstring), so it can only
    ever WARN here, never reproduce the real load-time hard fail.
    """
    from reyn.config.config_schema import RenamedKeyHint

    result: "dict[str, object]" = {}
    for key in raw:
        if key == "compaction":
            result[key] = RenamedKeyHint(
                note=(
                    "llm.model_class_by_purpose.compaction is no longer "
                    "configurable (#3785) — compaction always follows the "
                    "conversation's active model now. This key will make "
                    "reyn refuse to start (ValueError) — remove it from "
                    "your reyn.yaml/reyn.local.yaml."
                ),
            )
        elif key not in MODEL_CLASS_PURPOSES:
            result[key] = None
    return result


def _register_model_class_by_purpose_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator(
        "llm.model_class_by_purpose", _model_class_by_purpose_freeform_validator
    )


_register_model_class_by_purpose_validator()


def _register_retry_policy_open() -> None:
    """#4655 Kind② — ``llm.router.retry_policy`` already fails LOUDLY (a
    ``TypeError`` from ``litellm.RetryPolicy(**rcfg.retry_policy)`` at
    Router-build time, ``llm/llm.py``) on an unknown key — this is NOT the
    B-3 "silently accepted, does nothing" shape #4655 is about, so reyn
    does not need a second check ahead of that one.

    A prior revision of this registration WAS Kind① — it introspected
    ``litellm.RetryPolicy.model_fields`` to validate earlier, friendlier,
    at ``reyn config validate`` time. Reverted (lead-coder, #4665 review):
    two problems, not one. (1) It broke the litellm-boundary import seam
    (``tests/security/test_4421_litellm_import_seam.py`` — reyn code above the
    litellm layer must never import litellm types directly). (2) Even
    seam-compliant, it would have been reyn TAKING OVER a third party's
    vocabulary — "does reyn's code grow when litellm's RetryPolicy field
    set grows?" is yes, the exact test
    ``feedback_third_party_responsibility_is_not_ours_to_take_over``
    names. litellm already owns this enforcement and already fails
    loudly; reyn duplicating it would only be able to drift stale against
    upstream, never actually needed for the "silently ignored" defect
    class #4655 exists to close.
    """
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_open("llm.router.retry_policy")


_register_retry_policy_open()


def _gateway_surfaces_enabled_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — ``gateway.surfaces.enabled``'s finite vocabulary is the
    LIVE surface registry (``interfaces/web/surfaces.py``'s
    ``build_registry()``) — the exact same names ``resolve_enabled``
    resolves ``enabled.get(spec.name)`` against, so this can never drift
    from the real registry the way a hand-copied name list could.
    """
    from reyn.interfaces.web.surfaces import build_registry

    valid = frozenset(spec.name for spec in build_registry())
    return {key: None for key in raw if key not in valid}


def _register_gateway_surfaces_enabled_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator(
        "gateway.surfaces.enabled", _gateway_surfaces_enabled_freeform_validator
    )


_register_gateway_surfaces_enabled_validator()


def _entries_only_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — shared by ``pipelines`` / ``presentations``: each
    top-level dict-leaf's registry loader (``data/pipelines/registry.py``,
    ``data/presentations/registry.py``) reads ONLY ``raw.get("entries")``
    from it — no other top-level sub-key is ever consumed by either.
    NOT shared with ``skills`` — see
    :func:`_skills_freeform_validator` for why that one has a wider
    vocabulary."""
    return {key: None for key in raw if key != "entries"}


def _register_entries_only_validators() -> None:
    from reyn.config import config_schema

    for dotted_key in ("pipelines", "presentations"):
        config_schema.register_freeform_leaf_validator(
            dotted_key, _entries_only_freeform_validator
        )


_register_entries_only_validators()


#: #4655 — ``skills``' own vocabulary is WIDER than ``pipelines`` /
#: ``presentations``' bare ``{"entries"}``: ``config/loader.py``'s ``_merge``
#: (the ``skills`` branch) rides ``_provenance`` / ``_collisions`` INSIDE the
#: merged ``skills`` dict across config tiers — internal bookkeeping keys
#: ``reyn.interfaces.skill_invoke``'s ``:skill`` path reads to fire a loud
#: collision warning, not something an operator writes but real,
#: consumed data nonetheless (see ``loader.py``'s own comment on that
#: branch). Flagging them here would make a WELL-FORMED, freshly-loaded
#: config warn about its own internal bookkeeping — exactly the false
#: "not applied" #4515 already burned reyn once on.
_SKILLS_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "entries", "_provenance", "_collisions",
})


def _skills_freeform_validator(raw: dict) -> "dict[str, object]":
    """#4655 Kind① — see :data:`_SKILLS_TOP_LEVEL_KEYS`."""
    return {key: None for key in raw if key not in _SKILLS_TOP_LEVEL_KEYS}


def _register_skills_validator() -> None:
    from reyn.config import config_schema

    config_schema.register_freeform_leaf_validator("skills", _skills_freeform_validator)


_register_skills_validator()


def _register_declared_open_freeform_leaves() -> None:
    """#4655 Kind② — every OTHER free-form dict-leaf, verified genuinely
    open (consumed via ``.get(name)``/``.items()`` with a truly
    operator-chosen sub-key name — a model name, a provider name, a header
    name, ... — no bounded finite vocabulary exists to check). Each is a
    short, cited disposition, not a shrug: a leaf simply absent from every
    registration would be indistinguishable from nobody ever having looked
    at it, which is the exact defect #4655 exists to catch.
    """
    from reyn.config import config_schema

    # `permissions`: `PermissionResolver._is_config_approved`/
    # `_is_config_denied` (security/permissions/permissions.py) look up
    # `self._config.get(key)` for the CURRENT op's dotted key at runtime —
    # e.g. "web.fetch", f"http.get.{host}" (host is unboundedly open). The
    # valid key set is reyn's whole tool/capability catalog, resolved
    # elsewhere (permission decls, `ALL_OP_KINDS`-shaped op-kind names,
    # host names) — no single importable, already-enumerated catalog of
    # valid `permissions.*` keys exists anywhere in the codebase (the
    # closest candidate, `ALL_OP_KINDS`/`ALL_TOOL_NAMES`, is a DIFFERENT
    # vocabulary — op-kind names like "read_file", not permission-dotted
    # keys like "web.fetch" — mapping one to the other is hand-written
    # translation logic, not a genuine reuse of an existing catalog).
    # Building a new one would be a significant new abstraction, not a
    # "read one existing symbol" job, so this stays Kind②.
    config_schema.register_freeform_leaf_open("permissions")

    # `chat.compaction.section_weights`: unlike `component_weights`, its
    # own direct consumer (`services/compaction/engine.py`'s
    # `compute_budgets`, the `sw.items()` comprehension building
    # `section_caps`) reads EVERY key generically — no fixed set filters it
    # at this layer. A deeper indirection (the resulting `section_caps`
    # dict feeding an LLM-facing prompt hint that may not map to a real
    # summary section) is a real but DIFFERENT, deeper problem than the
    # "accepted but silently unused right here" shape #4655 covers.
    config_schema.register_freeform_leaf_open("chat.compaction.section_weights")

    # `llm.router.fallbacks`: `llm/llm.py` reads
    # `rcfg.fallbacks.get(original_model)` / `.get(model)` — keyed by
    # arbitrary operator-declared model names.
    config_schema.register_freeform_leaf_open("llm.router.fallbacks")

    # `llm.models`: `llm/model_resolver.py`'s `ModelResolver` resolves
    # every key present in the mapping — arbitrary operator-declared model
    # class names, no fixed set.
    config_schema.register_freeform_leaf_open("llm.models")

    # `auth.providers`: `_build_auth_config` (this module) iterates
    # `raw_providers.items()` — arbitrary operator-declared provider names,
    # each entry structurally validated but the NAME itself unconstrained.
    config_schema.register_freeform_leaf_open("auth.providers")

    # `observability.otel.headers`: `config/observability.py` builds
    # `{str(k): str(v) for k, v in headers_raw.items()}` — arbitrary HTTP
    # header names.
    config_schema.register_freeform_leaf_open("observability.otel.headers")

    # `cost.rate_limit_per_minute`: `runtime/budget/budget.py` reads
    # `self._config.rate_limit_per_minute.get(model)` — keyed by arbitrary
    # model name.
    config_schema.register_freeform_leaf_open("cost.rate_limit_per_minute")

    # `embedding.classes`: `config/embedding.py`'s parser iterates
    # `raw.items()` — arbitrary operator-declared embedding class names.
    config_schema.register_freeform_leaf_open("embedding.classes")


_register_declared_open_freeform_leaves()


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
        mode:
            #3823: which DEFAULT the resolved policy uses for an axis the
            operator left UNSET in ``policy`` below — never a direction the
            operator already wrote explicitly (``allow_X``/``deny_X``, or a
            bare bool axis, are ALWAYS honoured regardless of ``mode``; the
            operator's own explicit write is never second-guessed by this
            knob). ``'compat'`` (default) — every axis defaults to permitted
            (network on, subprocess on, nothing extra denied on read/write,
            everything passed through on env) — audit/events/timeout/
            cancel-teardown still apply regardless. ``'strict'`` — every axis
            EXCEPT write defaults to denied (network off, subprocess off,
            nothing passed through on env); write's default is UNAFFECTED by
            mode — it stays the caller-supplied workspace floor (an
            operator-unknowable value per-op, #3901 PR-B ①②; only an
            explicit ``allow_write_paths``/``deny_write_paths`` narrows it
            further). read has no mode-based default at all — there is no
            ``allow_read_paths`` concept (#1199 removed it from reyn's model
            entirely), so ``strict`` cannot narrow reads any tighter than
            ``compat`` already leaves them; only an explicit
            ``deny_read_paths`` narrows either mode. Allowed:
            ``{'compat', 'strict'}`` — not ``'custom'`` (owner ruling,
            2026-08-09: was never a third DIRECTION, it was the unresolved
            composition rule between ``mode`` and ``policy`` — #3823's rule
            above removes the need for it) and not ``'off'`` (expressible as
            ``'compat'`` with every axis left at its compat default).
            Resolution lives in
            :func:`reyn.security.sandbox.policy.resolve_sandbox_policy`.
        policy:
            The agent-level (operator) sandbox policy, in the CONFIG-facing
            vocabulary (#3823): ``network`` (bool) / ``subprocess`` (bool,
            positive framing — ``true`` = allowed) / ``allow_write_paths`` /
            ``deny_write_paths`` / ``deny_read_paths`` / ``allow_env_names``
            / ``deny_env_names`` / ``timeout_seconds`` / ``max_timeout_seconds``
            / ``max_output_bytes``
            — ``<direction>_<axis>_<unit>`` word order for a path/name-set
            axis, bare ``<axis>`` for a bool (tool-naming.md R1's word
            order, generalised). This vocabulary is DECOUPLED from
            ``SandboxPolicy``'s own internal field names (#3823; was a
            direct 1:1 transcription under #3901 PR-B ④ — see
            :func:`reyn.security.sandbox.policy._translate_sandbox_policy_config`
            for the translation and why the coupling, not the rename cost,
            was the thing to fix) — see :data:`_RENAMED_SANDBOX_POLICY_KEYS`
            for the old-key error an operator on a pre-#3823 (or pre-#3901)
            config sees; an unknown key not in either vocabulary raises,
            never resolves to "no restriction" silently. When set it is the
            deterministic policy the OS
            applies to sandboxed ops + the SandboxLayer of the permission ∩ —
            WINNING over op-declared fields (the LLM cannot widen it). ``None``
            (absent) means *no agent-level restriction* — the SandboxLayer stays
            ⊤ and op-level fields govern (the pre-#1326 default for any run that
            declares no policy). This replaces the retired phase-scoped
            ``default_sandbox_policy`` (FP-0017 remnant): sandbox authorization is
            an operator/run concern, not a per-phase one.
    """

    backend: str = "auto"
    on_unsupported: str = "warn"
    mode: str = DEFAULT_SANDBOX_MODE
    policy: dict | None = None
    # #4935: opt-in capability requirement (D1 companion to `enforced_axes`
    # — see `reyn.security.sandbox.capability`'s own module docstring for
    # the design). Default EMPTY: declaring nothing here changes nothing —
    # no run is stopped by this field unless the operator explicitly names
    # a capability they need. Each name must be one
    # `reyn.security.sandbox.capability.SANDBOX_CAPABILITY_NAMES` already
    # knows (today: just `"ipc_named_service"`) — an unknown name raises
    # here rather than silently resolving to "not required" (same
    # unknown-key-raises discipline `backend`/`on_unsupported`/`mode`
    # already apply below). When the RESOLVED backend declares a required
    # capability NOT_SUPPORTED, `on_unsupported` (the SAME 3-way knob
    # already used for "no backend available" — never a new vocabulary,
    # per owner's "don't make the operator learn a second mental model")
    # governs the response — see
    # `reyn.security.sandbox.policy.unsupported_required_capabilities`
    # for the resolution-time consumer.
    require_capabilities: "list[str]" = field(default_factory=list)

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
        if self.mode not in _SANDBOX_MODES:
            raise ValueError(
                f"sandbox.mode {self.mode!r} is not one of {sorted(_SANDBOX_MODES)}"
            )
        from reyn.security.sandbox.capability import SANDBOX_CAPABILITY_NAMES

        unknown_capabilities = set(self.require_capabilities) - SANDBOX_CAPABILITY_NAMES
        if unknown_capabilities:
            raise ValueError(
                f"sandbox.require_capabilities contains unknown name(s) "
                f"{sorted(unknown_capabilities)} — known: "
                f"{sorted(SANDBOX_CAPABILITY_NAMES)}"
            )
        # #4174 T0 (owner ruling — "no hard-fail anywhere, don't
        # special-case sandbox.policy"): this used to also fail-fast on an
        # unknown/renamed policy key (a renamed-key guard against
        # _RENAMED_SANDBOX_POLICY_KEYS, then a translate+construct
        # round-trip against SandboxPolicy). Both raises are REMOVED —
        # sandbox.policy is no longer a special case among config sections,
        # it goes through the same unified warn-not-fail unknown-key path
        # every other section does (see the config_schema.
        # register_freeform_leaf_validator("sandbox.policy", ...) call
        # below, and _translate_sandbox_policy_config's own docstring for
        # the runtime-translation side of this same change). Only the
        # structural "policy must be a mapping" check stays here — that one
        # isn't a vocabulary question the unified walk can answer, since a
        # non-dict value has no keys to check at all.
        if self.policy is not None and not isinstance(self.policy, dict):
            raise ValueError(
                f"sandbox.policy must be a mapping, got {type(self.policy).__name__}"
            )


def _build_sandbox_config(raw: object) -> SandboxConfig:
    """Parse the ``sandbox:`` section. Empty / missing returns SandboxConfig()."""
    if not isinstance(raw, dict):
        return SandboxConfig()
    defaults = SandboxConfig()
    backend = str(raw.get("backend", defaults.backend))
    on_unsupported = str(raw.get("on_unsupported", defaults.on_unsupported))
    mode = str(raw.get("mode", defaults.mode))
    # #1326: optional agent-level policy. Absent → None (SandboxLayer stays ⊤).
    policy_raw = raw.get("policy")
    policy = dict(policy_raw) if isinstance(policy_raw, dict) else None
    # #4935: opt-in capability requirement. Absent/empty → [] (no run
    # affected) — see SandboxConfig's own field docstring.
    require_capabilities_raw = raw.get("require_capabilities")
    require_capabilities = (
        [str(c) for c in require_capabilities_raw]
        if isinstance(require_capabilities_raw, list)
        else list(defaults.require_capabilities)
    )
    # Validation delegated to __post_init__ — raises ValueError with clear message.
    return SandboxConfig(
        backend=backend,
        on_unsupported=on_unsupported,
        mode=mode,
        policy=policy,
        require_capabilities=require_capabilities,
    )


@dataclass
class CronJobConfig:
    """One ``cron.jobs[]`` entry (FP-0009 Component B + FP-0041 #489 PR-B;
    ``action`` field #5209).

    Maps directly onto ``CronJob`` consumed by ``CronScheduler``; this
    config-side dataclass exists to keep the YAML parsing layer
    independent of the runtime layer.

    ``to`` (target agent) is required for EVERY job regardless of
    ``action`` — it names the agent whose ``cron:<job_name>`` Session this
    job runs on (the "host"), which ``resolve_cron_session`` /
    ``dispatch_cron_fired`` need to resolve a Session to fire
    ``cron_fired`` on at all; there is no session-less dispatch path.
    ``action`` declares what a fire actually DOES on that host session:

      - ``"message"`` (default, unchanged): ``to`` doubles as the message
        RECIPIENT too — ``message`` (free-form text) is required, dispatched
        to the agent's inbox as a normal attributed turn (= always starts an
        LLM turn).
      - ``"hook"`` (#5209): only fires the ``cron_fired`` external-event hook
        on the host session — never starts a turn itself. Whether anything
        happens next is entirely up to a hooks.yaml ``on: cron_fired``
        entry's own ``push_when`` (an ``exec_capture`` hook whose script
        decides token-0 vs push, #5209's own reason to exist). ``message``
        set on a ``"hook"`` job is a config error — a hook job never has
        text to deliver, so a written ``message`` would silently be
        ignored without this check (#5209, architect ruling: declare the
        action positively, never let an absent field carry meaning).

    Unknown ``action`` values are rejected at load, same as a missing
    ``to``/``message`` shape.
    """

    name: str
    schedule: str   # 5-field cron expression
    to: str | None = None        # target agent name — required for every action (see class docstring)
    message: str | None = None   # free-form text dispatched to the agent's inbox — action="message" only
    action: str = "message"      # #5209: "message" (default) | "hook"
    notify: str | None = None    # FP-0043 S4b-3b: opt-in notify channel (e.g. "telegram"); None = event-log only
    input: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class CronConfig:
    """``cron:`` — scheduled message dispatch (FP-0009 Component B).

    Each entry under ``cron.jobs`` dispatches a message to a target agent
    on a cron schedule via ``CronScheduler`` (= attached to
    ``app.state.cron_scheduler`` in web mode, or run foreground via
    ``reyn cron run``).
    """

    jobs: list[CronJobConfig] = field(default_factory=list)


def _build_cron_config(raw: object) -> CronConfig:
    """Parse the ``cron:`` section from reyn.yaml / ``.reyn/cron.yaml``.

    Shape (FP-0009 + FP-0041 #489 PR-B; ``action`` #5209)::

        cron:
          jobs:
            - name: morning_news
              to: news_agent
              message: "今日の主要ニュースをまとめて"
              schedule: "0 9 * * *"
              enabled: true
            - name: check_deploy_status   # action: hook — no turn unless the
              to: ops_agent               # hook's own push_when says so
              action: hook
              schedule: "*/5 * * * *"

    ``None`` / missing block / empty dict → ``CronConfig(jobs=[])``.
    Validates ``name`` + ``schedule`` are non-empty strings + ``to`` is
    always set + ``action``-appropriate shape per entry (#5209:
    ``action: message`` needs ``message`` too; ``action: hook`` rejects a
    ``message``), raising ``ValueError`` naming the offending entry on
    failure. Unknown extra fields are ignored (= forward-compatible).
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
        schedule = entry.get("schedule")
        if not schedule or not isinstance(schedule, str):
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): 'schedule' must be a non-empty string "
                f"(got {schedule!r})"
            )
        # #5209: action declares what a fire does; to is required either
        # way (the host session cron_fired resolves against).
        action = entry.get("action", "message")
        if action not in ("message", "hook"):
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): 'action' must be "
                f"'message' or 'hook' (got {action!r})"
            )
        to = entry.get("to")
        if not to or not isinstance(to, str):
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): 'to' must be a non-empty "
                f"string (the agent this job runs on — required for every "
                f"action, got {to!r})"
            )
        message = entry.get("message")
        if action == "message":
            # FP-0041 #489 PR-B: a message job must set 'message' too.
            if not message or not isinstance(message, str):
                raise ValueError(
                    f"cron.jobs[{i}] (name={name!r}): action='message' "
                    f"requires a non-empty 'message' (got {message!r})"
                )
        else:  # action == "hook"
            # #5209: a hook job never delivers text — a written 'message'
            # would silently be ignored without this check.
            if message is not None:
                raise ValueError(
                    f"cron.jobs[{i}] (name={name!r}): action='hook' must "
                    f"not set 'message' (it is never delivered — got "
                    f"{message!r})"
                )
        raw_input = entry.get("input") or {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        enabled = bool(entry.get("enabled", True))
        # FP-0043 S4b-3b: opt-in notify channel.
        notify = entry.get("notify")
        notify = notify if (isinstance(notify, str) and notify) else None
        jobs.append(CronJobConfig(
            name=name,
            schedule=schedule,
            to=to,
            message=message,
            action=action,
            notify=notify,
            input=dict(raw_input),
            enabled=enabled,
        ))
    return CronConfig(jobs=jobs)


@dataclass
class FsWatchConfig:
    """``fs_watch:`` — operator-declared filesystem watch paths (#2608 H4).

    Each entry in ``paths`` is a directory watched (recursively) for
    create/modify/delete events; a change fires the ``file_changed``
    external-event hook (see ``reyn.runtime.fs_watcher.FsWatcher`` and
    ``reyn.hooks.schema.ALLOWED_HOOK_POINTS``).

    SECURITY (F7-5, do not relitigate): this is the ONLY place watched paths
    are declared. There is no runtime op or tool verb that lets an agent
    register or widen a watch — like ``sandbox:``'s policy, ``fs_watch:``
    is OUT-set-only (restart-only, ``reyn.yaml``/``reyn.local.yaml``, never
    a ``.reyn/*.yaml`` hot-reload file — see ``config/loader.py``'s
    ``_HOT_RELOAD_FILES``), so an LLM-driven config-write op can never touch
    it. Letting an agent name arbitrary watch paths would be an
    info-gathering surface (a filesystem-wide change-notification feed) —
    same class of concern as sandbox policy, hence the same OUT-set gate.
    """

    paths: list[str] = field(default_factory=list)
    debounce_seconds: float = 0.2


def _build_fs_watch_config(raw: object) -> FsWatchConfig:
    """Parse the ``fs_watch:`` section from ``reyn.yaml``/``reyn.local.yaml``
    (OUT-set only — see :class:`FsWatchConfig`'s docstring).

    Shape::

        fs_watch:
          paths:
            - /repo/src
            - /repo/docs
          debounce_seconds: 0.2   # optional, default 0.2

    ``None`` / missing block / empty dict / non-dict → ``FsWatchConfig()``
    (``paths=[]`` — the watcher never starts; see
    ``reyn.runtime.fs_watcher.FsWatcher.start``'s no-op-when-empty
    contract). Non-string / blank entries in ``paths`` are dropped
    (forward-compatible / defensive — never raises on a malformed entry).
    """
    if not isinstance(raw, dict):
        return FsWatchConfig()
    raw_paths = raw.get("paths") or []
    if not isinstance(raw_paths, list):
        raw_paths = []
    paths = [p for p in (str(p).strip() for p in raw_paths if p) if p]
    debounce = raw.get("debounce_seconds", 0.2)
    try:
        debounce_seconds = float(debounce)
    except (TypeError, ValueError):
        debounce_seconds = 0.2
    return FsWatchConfig(paths=paths, debounce_seconds=debounce_seconds)


def _build_audit_events_config(raw: object) -> AuditEventsConfig:
    """Parse `audit_events:` from reyn.yaml (#4174 T5, renamed from `events:`).

    #4479: `cleanup_period_days` / `max_disk_usage_percent` — 0 disables
    that axis (owner ruling — the OPPOSITE of this field's earlier stance,
    which rejected 0 as a footgun; see `AuditEventsConfig`'s own docstring
    for why the reversal). A negative or non-numeric value falls back to
    the default rather than being accepted or rejected outright — same
    discipline as every other numeric config builder in this module (an
    operator typo must not silently produce a nonsensical negative purge
    threshold)."""
    defaults = AuditEventsConfig()
    if not isinstance(raw, dict):
        return defaults
    cleanup = raw.get("cleanup_period_days", defaults.cleanup_period_days)
    try:
        cleanup_val = int(cleanup)
        if cleanup_val < 0:
            cleanup_val = defaults.cleanup_period_days
    except (TypeError, ValueError):
        cleanup_val = defaults.cleanup_period_days
    disk_percent = raw.get("max_disk_usage_percent", defaults.max_disk_usage_percent)
    try:
        disk_percent_val = float(disk_percent)
        if disk_percent_val < 0:
            disk_percent_val = defaults.max_disk_usage_percent
    except (TypeError, ValueError):
        disk_percent_val = defaults.max_disk_usage_percent
    # #4496 PR-2: `network` is a declared future value, not yet backed by an
    # implementation (see AuditEventsConfig.backend's own docstring) — an
    # operator who sets it (or any other unrecognized string) falls back
    # to the default rather than reaching `EventLog` with a value nothing
    # can resolve to a real backend, same "malformed value falls back"
    # discipline every other field in this parser already uses.
    backend_val = raw.get("backend", defaults.backend)
    if backend_val not in ("local", "discard"):
        backend_val = defaults.backend
    # #4960: same "malformed/non-positive falls back to the measured
    # default" discipline as every other numeric field in this parser —
    # an operator typo must not silently produce "coalesce every 0
    # fragments" (== effectively unthrottled again, defeating the point).
    coalesce_fragments = raw.get(
        "agent_delta_coalesce_fragments", defaults.agent_delta_coalesce_fragments,
    )
    try:
        coalesce_fragments_val = int(coalesce_fragments)
        if coalesce_fragments_val <= 0:
            coalesce_fragments_val = defaults.agent_delta_coalesce_fragments
    except (TypeError, ValueError):
        coalesce_fragments_val = defaults.agent_delta_coalesce_fragments
    coalesce_interval_ms = raw.get(
        "agent_delta_coalesce_interval_ms", defaults.agent_delta_coalesce_interval_ms,
    )
    try:
        coalesce_interval_ms_val = int(coalesce_interval_ms)
        if coalesce_interval_ms_val <= 0:
            coalesce_interval_ms_val = defaults.agent_delta_coalesce_interval_ms
    except (TypeError, ValueError):
        coalesce_interval_ms_val = defaults.agent_delta_coalesce_interval_ms
    # #4975: same "malformed/non-positive falls back to the default"
    # discipline as every other numeric field above.
    provider_body_max_chars = raw.get(
        "provider_body_max_chars", defaults.provider_body_max_chars,
    )
    try:
        provider_body_max_chars_val = int(provider_body_max_chars)
        if provider_body_max_chars_val <= 0:
            provider_body_max_chars_val = defaults.provider_body_max_chars
    except (TypeError, ValueError):
        provider_body_max_chars_val = defaults.provider_body_max_chars
    return AuditEventsConfig(
        max_bytes=int(raw.get("max_bytes", defaults.max_bytes)),
        max_age_seconds=int(raw.get("max_age_seconds", defaults.max_age_seconds)),
        cleanup_period_days=cleanup_val,
        max_disk_usage_percent=disk_percent_val,
        backend=backend_val,
        agent_delta_coalesce_fragments=coalesce_fragments_val,
        agent_delta_coalesce_interval_ms=coalesce_interval_ms_val,
        # #4666: same `bool(raw.get(...))` convention every other boolean
        # field in this module's parsers already uses.
        agent_delta_include_text=bool(
            raw.get("agent_delta_include_text", defaults.agent_delta_include_text)
        ),
        # #4666②: same convention.
        completed_response_include_text=bool(
            raw.get(
                "completed_response_include_text",
                defaults.completed_response_include_text,
            )
        ),
        # #4666 item ③: same convention, separate knob.
        user_input_include_text=bool(
            raw.get("user_input_include_text", defaults.user_input_include_text)
        ),
        # #4975: same convention, separate knob (its own opt-in on top of
        # the 3 above — see the field's own docstring for why).
        provider_body_include_text=bool(
            raw.get("provider_body_include_text", defaults.provider_body_include_text)
        ),
        provider_body_max_chars=provider_body_max_chars_val,
    )


def _build_storage_config(raw: object) -> StorageConfig:
    """Parse `storage:` from reyn.yaml (#5366).

    A malformed (non-numeric, non-positive) `max_bytes` falls back to
    the field's own default (``None`` — unlimited). Unlike
    ``ArtifactsConfig``'s numeric fallback (where the default IS the
    cap, so falling back never disables it), this field's own default
    already means "the cap is off" by design (architect's ruling:
    unlimited is the field's OWN steady state, not a failure mode) — a
    malformed value therefore falls back to the SAME state an operator
    who wrote nothing gets, not a silent widening of an otherwise-active
    cap."""
    defaults = StorageConfig()
    if not isinstance(raw, dict):
        return defaults
    max_bytes_raw = raw.get("max_bytes", defaults.max_bytes)
    max_bytes_val: "int | None" = defaults.max_bytes
    if max_bytes_raw is not None:
        try:
            candidate = int(max_bytes_raw)
            if candidate > 0:
                max_bytes_val = candidate
        except (TypeError, ValueError):
            pass
    pin_raw = raw.get("pin", defaults.pin)
    pin_val = (
        [p for p in pin_raw if isinstance(p, str)]
        if isinstance(pin_raw, list)
        else list(defaults.pin)
    )
    return StorageConfig(max_bytes=max_bytes_val, pin=pin_val)


def _build_artifacts_config(raw: object) -> ArtifactsConfig:
    """Parse `artifacts:` from reyn.yaml (#4601).

    A non-positive or non-numeric `remote_fallback_limit` falls back to
    the default — same "malformed value falls back, never disables the
    cap outright" discipline every other numeric field in this module
    uses (an operator typo must not silently produce an unbounded
    fallback again, the exact defect #4601 exists to close)."""
    defaults = ArtifactsConfig()
    if not isinstance(raw, dict):
        return defaults
    limit = raw.get("remote_fallback_limit", defaults.remote_fallback_limit)
    try:
        limit_val = int(limit)
        if limit_val <= 0:
            limit_val = defaults.remote_fallback_limit
    except (TypeError, ValueError):
        limit_val = defaults.remote_fallback_limit
    return ArtifactsConfig(remote_fallback_limit=limit_val)


