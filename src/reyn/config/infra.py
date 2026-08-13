"""reyn.config.infra — infra config: AgentId/Auth/Sandbox/AuditEvents/Eval/Cron/Python. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


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
    from reyn.security.secrets.oauth import OAuthProviderConfig

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
    # Validation delegated to __post_init__ — raises ValueError with clear message.
    return SandboxConfig(
        backend=backend, on_unsupported=on_unsupported, mode=mode, policy=policy
    )


@dataclass
class CronJobConfig:
    """One ``cron.jobs[]`` entry (FP-0009 Component B + FP-0041 #489 PR-B).

    Maps directly onto ``CronJob`` consumed by ``CronScheduler``; this
    config-side dataclass exists to keep the YAML parsing layer
    independent of the runtime layer.

    Jobs are message-based (= FP-0041 #489 PR-B): ``to`` (target agent) +
    ``message`` (free-form text). Cron dispatches the message to the target
    agent's inbox with a ``sender="cron:<name>"`` envelope.

    (A bare name without ``to`` + ``message`` is not a valid job shape;
    an entry missing those fields is rejected at load with a ValueError naming it.)
    """

    name: str
    schedule: str   # 5-field cron expression
    to: str | None = None        # target agent name
    message: str | None = None   # free-form text dispatched to the agent's inbox
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

    Shape (FP-0009 + FP-0041 #489 PR-B)::

        cron:
          jobs:
            - name: morning_news
              to: news_agent
              message: "今日の主要ニュースをまとめて"
              schedule: "0 9 * * *"
              enabled: true

    ``None`` / missing block / empty dict → ``CronConfig(jobs=[])``.
    Validates ``name`` + ``schedule`` are non-empty strings + ``to`` +
    ``message`` are set per entry, raising ``ValueError`` naming the
    offending entry on failure. An entry without the ``to`` + ``message``
    shape (entry missing ``to`` + ``message``) is rejected with that
    ValueError. Unknown extra fields are ignored (= forward-compatible).
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
        # FP-0041 #489 PR-B: a job must be message-based (to + message). An
        # entry lacking that shape is rejected below.
        to = entry.get("to")
        message = entry.get("message")
        has_message_shape = (
            bool(to) and isinstance(to, str)
            and bool(message) and isinstance(message, str)
        )
        if not has_message_shape:
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): must set "
                f"'to' + 'message'."
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
    return AuditEventsConfig(
        max_bytes=int(raw.get("max_bytes", defaults.max_bytes)),
        max_age_seconds=int(raw.get("max_age_seconds", defaults.max_age_seconds)),
        cleanup_period_days=cleanup_val,
        max_disk_usage_percent=disk_percent_val,
        backend=backend_val,
    )


