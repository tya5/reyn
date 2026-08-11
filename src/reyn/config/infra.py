"""reyn.config.infra — infra config: Agent/Auth/Sandbox/Events/Eval/Cron/Python. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


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
    # #1829 S4: credential rotation. model_name → list of credential refs, each
    # ``{"api_key_env": "<ENV_VAR_NAME>"}``. Each usable ref becomes a Router
    # deployment with the SAME model_name + that key, so the Router rotates / fails
    # over across keys. Keys are referenced by ENV-VAR NAME only — NEVER inline a
    # key value here (the value is read from os.environ at build time and is never
    # logged / fingerprinted; only the NAME is).
    credentials: dict = field(default_factory=dict)
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
    """``llm:`` — LLM-layer config (#1829 router, #1835 retry)."""

    router: RouterConfig = field(default_factory=RouterConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)


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
    cred = raw.get("credentials", d.credentials)
    if cred and not isinstance(cred, dict):
        raise ValueError(
            "llm.router.credentials must be a mapping (model → [{api_key_env: NAME}]), "
            f"got {type(cred).__name__}"
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
        # ENV-VAR NAMES only (never key values). A non-dict entry / missing
        # api_key_env is dropped here; an all-unusable model is caught at build.
        credentials={
            str(m): [
                {"api_key_env": str(c["api_key_env"])}
                for c in (lst or [])
                if isinstance(c, dict) and c.get("api_key_env")
            ]
            for m, lst in (cred or {}).items()
        },
        # None → absent (default litellm behavior); mapping → constructed at Router
        # build time into a litellm.RetryPolicy object.
        retry_policy={str(k): int(v) for k, v in rp_raw.items()} if rp_raw else None,
    )


def _build_llm_config(raw: object) -> LLMConfig:
    """Parse ``llm:`` from reyn.yaml. None/missing → defaults."""
    if raw is None:
        return LLMConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"llm must be a mapping, got {type(raw).__name__}")
    return LLMConfig(
        router=_build_router_config(raw.get("router")),
        retry=_build_retry_config(raw.get("retry")),
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
class PythonConfig:
    """`python` section — settings for the python preprocessor step."""
    # Modules that user code may import in pure mode in addition to the
    # stdlib allowlist. Curate carefully: libraries that internally do I/O
    # (pandas.read_csv, requests, etc.) defeat pure-mode sandboxing.
    allowed_modules: list[str] = field(default_factory=list)
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
    is the single-run mode (1 run = 1 file).

    `cleanup_period_days` documents how long closed files should be kept
    before `reyn events purge` may delete them. `null` (default) disables
    automatic deletion — purge only runs when invoked explicitly. Setting
    `0` is rejected (it is a footgun: Claude Code historically treated
    `0` as "disable transcript writes" and surprised users).
    """
    max_bytes: int = 10 * 1024 * 1024     # 10 MB
    max_age_seconds: int = 24 * 60 * 60   # 1 day
    cleanup_period_days: int | None = None


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


def _build_python_config(raw: object) -> PythonConfig:
    if not isinstance(raw, dict):
        return PythonConfig()
    modules = raw.get("allowed_modules") or []
    if not isinstance(modules, list):
        modules = []
    return PythonConfig(allowed_modules=[str(m) for m in modules])


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


