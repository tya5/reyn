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


_SANDBOX_BACKENDS = {"auto", "seatbelt", "landlock", "noop"}
_SANDBOX_ON_UNSUPPORTED = {"warn", "error", "ignore"}


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
        policy:
            The agent-level (operator) sandbox policy: a mapping of
            ``SandboxPolicy`` kwargs (``network`` / ``write_paths`` /
            ``read_deny_paths`` / ``read_paths`` / ``allow_subprocess`` /
            ``env_passthrough`` / ``timeout_seconds``). When set it is the
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
        if self.policy is not None:
            # Fail-fast on a malformed operator policy: construct a SandboxPolicy
            # to validate the keys (unknown key → TypeError → clear ValueError).
            from reyn.security.sandbox.policy import SandboxPolicy

            if not isinstance(self.policy, dict):
                raise ValueError(
                    f"sandbox.policy must be a mapping, got {type(self.policy).__name__}"
                )
            try:
                SandboxPolicy(**self.policy)
            except TypeError as exc:
                raise ValueError(f"sandbox.policy is invalid: {exc}") from exc


def _build_sandbox_config(raw: object) -> SandboxConfig:
    """Parse the ``sandbox:`` section. Empty / missing returns SandboxConfig()."""
    if not isinstance(raw, dict):
        return SandboxConfig()
    defaults = SandboxConfig()
    backend = str(raw.get("backend", defaults.backend))
    on_unsupported = str(raw.get("on_unsupported", defaults.on_unsupported))
    # #1326: optional agent-level policy. Absent → None (SandboxLayer stays ⊤).
    policy_raw = raw.get("policy")
    policy = dict(policy_raw) if isinstance(policy_raw, dict) else None
    # Validation delegated to __post_init__ — raises ValueError with clear message.
    return SandboxConfig(
        backend=backend, on_unsupported=on_unsupported, policy=policy
    )


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
class CronJobConfig:
    """One ``cron.jobs[]`` entry (FP-0009 Component B + FP-0041 #489 PR-B).

    Maps directly onto ``CronJob`` consumed by ``CronScheduler``; this
    config-side dataclass exists to keep the YAML parsing layer
    independent of the runtime layer.

    Two execution shapes co-exist (= FP-0041 #489 PR-B):

      - **Message-based** (recommended): ``to`` + ``message``. Cron
        dispatches the message to the target agent's inbox with
        ``sender="cron:<name>"`` envelope.
      - **Skill-based** (legacy): ``skill``. Cron runs the skill
        directly via ``Agent.run`` (= FP-0009 original shape).

    Exactly one shape per job. Validation rejects both / neither.
    """

    name: str
    schedule: str   # 5-field cron expression
    to: str | None = None        # message-based: target agent name
    message: str | None = None   # message-based: free-form text
    skill: str | None = None     # skill-based legacy: skill name
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
    """Parse the ``cron:`` section from reyn.yaml / ``.reyn/cron.yaml``.

    Shape (FP-0009 + FP-0041 #489 PR-B)::

        cron:
          jobs:
            # Message-based (FP-0041 recommended):
            - name: morning_news
              to: news_agent
              message: "今日の主要ニュースをまとめて"
              schedule: "0 9 * * *"
              enabled: true

            # Skill-based (FP-0009 legacy, backward compat):
            - name: index_events_hourly
              skill: index_events
              schedule: "0 */6 * * *"
              input: {}
              enabled: true

    ``None`` / missing block / empty dict → ``CronConfig(jobs=[])``.
    Validates ``name`` + ``schedule`` are non-empty strings + exactly
    one of (``skill``) OR (``to`` + ``message``) is set per entry.
    Raises ``ValueError`` naming the offending entry on validation
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
        # FP-0041 #489 PR-B: shape selection — message-based vs skill-based.
        skill = entry.get("skill")
        to = entry.get("to")
        message = entry.get("message")
        has_skill = bool(skill) and isinstance(skill, str)
        has_message_shape = (
            bool(to) and isinstance(to, str)
            and bool(message) and isinstance(message, str)
        )
        if has_skill and has_message_shape:
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): cannot set both "
                f"'skill' and 'to'/'message' (= choose one shape)."
            )
        if not has_skill and not has_message_shape:
            raise ValueError(
                f"cron.jobs[{i}] (name={name!r}): must set either "
                f"'skill' (legacy) OR 'to' + 'message' (= recommended)."
            )
        raw_input = entry.get("input") or {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        enabled = bool(entry.get("enabled", True))
        jobs.append(CronJobConfig(
            name=name,
            schedule=schedule,
            to=to if has_message_shape else None,
            message=message if has_message_shape else None,
            skill=skill if has_skill else None,
            input=dict(raw_input),
            enabled=enabled,
        ))
    return CronConfig(jobs=jobs)


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


