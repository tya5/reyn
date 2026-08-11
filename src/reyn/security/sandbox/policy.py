"""SandboxPolicy — declarative description of what a sandboxed exec may do.

The policy is data only: declared in the agent profile (= FP-0017) and passed by the OS
to a SandboxBackend. P3/P7-aligned — the policy is mechanism-agnostic; backend
selection lives in `reyn.security.sandbox.backend.get_default_backend()`.

Scoping model (#1199 realignment, per-axis; #3901 PR-B ④ compat-default ruling
layered on top — see the ``SandboxPolicy`` class docstring for the full
rationale):
    write   — tight workspace-allowlist (``write_paths``) = the hard guard,
              the one axis that stays closed by default (operator-unknowable).
    network — compat (default open) — owner decision 2026-06-05 established
              this at the ``resolve_sandbox_policy`` floor (see
              :data:`DEFAULT_SANDBOX_NETWORK`), and #3901's later full-compat
              ruling extends the same posture to every other axis: the
              sandbox no longer re-decides what the launching shell could
              already do; an operator who wants isolation sets it off
              explicitly.
    exec    — compat (``deny_subprocess`` defaults False = spawning allowed).
    read    — **broad-allow by default**, unchanged since #1199: the strict
              read-allowlist was abolished, and a ``read_deny_paths``
              defense-in-depth carve-out is available (now empty by default,
              per the owner's #3901 compat ruling) for an operator who wants
              specific credential locations denied.

★ #3905: this section previously said "network — tight (default off /
allowlist)", which was true of #1199's ORIGINAL design but went stale when
the owner flipped the RESOLVED default to ON (2026-06-05) without updating
this docstring — the dataclass's own field default carried the SAME
staleness until #3905 aligned it with :data:`DEFAULT_SANDBOX_NETWORK`, the
single source both now read from (#3901 PR-B ④ then generalised that
single-source, compat-by-default posture to every other axis).

Fields:
    network: allow outbound network access from the sandboxed process.
        Defaults to :data:`DEFAULT_SANDBOX_NETWORK` (True) — see that
        constant's own docstring for the owner decision and rationale.
    write_paths: filesystem paths the process may write (write implies read).
        ``~`` is expanded (see :func:`expand_policy_path`).
    read_deny_paths: sensitive paths to DENY from the broad read surface
        (defense-in-depth, opt-in). Enforced where the backend can express a
        deny-after-allow rule (Seatbelt / SBPL); NOT enforceable on
        allowlist-only backends (Landlock), which rely on the network gate.
        Empty by default (#3901 PR-B ④); ``~`` is expanded.
    write_deny_paths: the write axis's own deny-list (#3901 PR-B ④), mirroring
        ``read_deny_paths``. Empty by default; ``~`` is expanded.
    deny_subprocess: whether the process may NOT spawn children. Defaults to
        False = spawning allowed (owner decision, 2026-07-22, #3202, restated
        as a deny-list-shaped bool by #3901 PR-B ④) — an explicit True at any
        call site still denies.
    env_deny_names: env-var names to withhold from the sandboxed process
        (#3901 PR-B ④, a deny-list — empty means the whole environment passes
        through, same trust level as the launching shell).
    timeout_seconds: foreground exec wall-clock DEFAULT (enforced by the
        backend), applied when the LLM's call omits its own timeout.
    max_timeout_seconds: foreground exec's LLM-extensible CEILING — the LLM
        may request up to this, never past it.
    background_timeout_seconds: background exec's OWN default (#3903 a-2) —
        a distinct value from ``timeout_seconds``, not the same field reused.
    background_max_timeout_seconds: background exec's OWN ceiling, ``int |
        None`` — ``None`` (the default) means unbounded.
    max_output_bytes: per-stream cap (bytes) on captured stdout/stderr — output
        beyond it is drained-and-discarded (the ``truncated`` flag is set) so a
        flooding child cannot exhaust host memory. Default 10 MiB; overridable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES

_log = logging.getLogger(__name__)


def expand_policy_path(raw: str) -> Path:
    """Expand a leading ``~`` in a policy path — the SHARED contract every
    backend must apply to every path field it enforces (#2976).

    Policy paths are operator-authored (``reyn.yaml sandbox.policy``, an MCP
    server's ``write_paths``), so ``~/.npm`` is the natural way to write a
    home-relative path and MUST mean ``$HOME/.npm``.

    This exists because the expansion was applied to ``read_deny_paths`` but
    NOT to ``write_paths``: ``Path("~/.npm").resolve()`` silently yields
    ``<cwd>/~/.npm`` — a literal ``~`` directory that does not exist. The grant
    was therefore emitted for a path the process never touches, so the write
    stayed denied while the policy object *looked* correct. Nothing failed
    loudly; the only symptom was an opaque ``Operation not permitted`` from a
    path the operator had explicitly allowed.

    Deliberately does NOT call ``resolve()``: symlink resolution is a separate,
    backend-specific decision (Seatbelt resolves for SBPL subpath matching;
    Landlock hands the path to the kernel as-is). Callers add it where they
    need it, so this helper stays no more opinionated than its least
    opinionated caller.
    """
    return Path(raw).expanduser()

# OS-level sensitive paths denied from the broad read surface by default
# (defense-in-depth). These are universal credential / secret store locations,
# not domain-specific (P7 ok) — the same class as the system-bootstrap paths a
# backend always allows. A policy may override ``read_deny_paths`` to widen or
# narrow this set. Workspace-internal secrets (e.g. a project ``.env``) are
# intentionally NOT in the default — the agent operates inside the workspace,
# so a blanket workspace deny would break legitimate reads; an operator who
# needs it can add the path explicitly.
DEFAULT_SENSITIVE_READ_DENY: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker/config.json",
    "~/.netrc",
)


def resolve_passthrough_env(policy: "SandboxPolicy") -> dict[str, str]:
    """Build the env dict every sandbox backend passes to a spawned child
    (#3075 fix 5 — the shared chokepoint all three backends call).

    #3901 PR-B ④ (owner ruling B, full compat): the WHOLE of ``os.environ``
    passes through MINUS ``policy.env_deny_names`` — a real behavior change
    from the prior allow-list shape (``policy.env_passthrough`` ∪ the
    standard proxy/CA set only). Before this: a sandboxed child could NOT
    see ``OPENAI_API_KEY``-shaped vars unless an operator explicitly
    declared them. After: it can, unless explicitly denied — "the same
    trust level as running the command in your own shell" (#3901's
    guiding UX principle), the same one #3202 already applied to
    ``allow_subprocess``/``network``, now extended to env by the owner's
    full-compat ruling rather than left as a partial application.

    #3823: when ``policy.allow_env_names`` is not ``None``, the axis SWITCHES
    to allow-list semantics — only those names pass through, still with
    ``env_deny_names`` intersected on top (deny always wins, same rule as
    every other axis: an operator who both allows and denies the same name
    gets the deny). ``None`` (the default; unaffected by this switch) keeps
    the deny-list-only behavior above.

    PATH fallback is applied by each backend after calling this (preserves the
    existing "PATH always available" behaviour independent of this set).
    """
    deny = set(policy.env_deny_names)
    if policy.allow_env_names is not None:
        allow = set(policy.allow_env_names)
        return {
            name: value for name, value in os.environ.items()
            if name in allow and name not in deny
        }
    return {name: value for name, value in os.environ.items() if name not in deny}


# The network default lives in ONE place so the owner can flip it trivially
# (no hardcode scatter — #3905: this used to ALSO be hardcoded separately as
# the SandboxPolicy dataclass field's own default, which drifted out of sync
# with this constant when the owner changed it and nobody noticed until the
# mismatch itself derailed a design discussion). Owner decision 2026-06-05:
# default ON (the operator, not the LLM, owns the policy; an operator who
# wants isolation sets it off via reyn.yaml sandbox.policy). Used by the
# dataclass default below, the chat factories, and MCP wrap.
DEFAULT_SANDBOX_NETWORK: bool = True


# #3903①: the foreground exec wall-clock timeout — a boundedness axis, not a
# permission axis (#3907/#3962 scoped the operator-only "LLM cannot set it"
# rule to the 5 PERMISSION fields + timeout separately; timeout's own
# boundedness is a DIFFERENT axis whose LLM-facing extensibility is owner-
# ruled — SandboxedExecIROp.timeout_seconds is back, LLM-settable, since
# #3962 removed it). Owner ruling (2026-08-11): raise the prior 60s default
# to 120s / let the LLM extend up to 600s, matching industry precedent
# (Claude Code: 120s default foreground, 600s max) rather than a measured
# need — "#3903 is about realizing a SPEC, not something a measurement
# should block."
#
# architect ruling (2026-08-11, conditional approval of the #3962 reversal):
# ``timeout_seconds`` is ALREADY operator-settable
# (``_SANDBOX_POLICY_CONFIG_KEY_TO_FIELD``) — if the LLM could always widen
# it to a HARDCODED 600 regardless of what the operator configured, the LLM
# would be widening the operator's own envelope, a Security pass-line
# violation (an operator who set a tighter cap would have it silently
# defeated). So DEFAULT_EXEC_TIMEOUT_SECONDS/DEFAULT_MAX_EXEC_TIMEOUT_SECONDS
# below are DEFAULTS ONLY — what ``SandboxPolicy.timeout_seconds`` /
# ``.max_timeout_seconds`` resolve to when the OPERATOR leaves them unset —
# never hardcoded ceilings a running policy is measured against. The LLM's
# actual ceiling on any given deployment is ``policy.max_timeout_seconds``,
# whatever the operator configured it to (600 only when they left it at the
# default). Single source: SandboxPolicy's own field defaults below and the
# LLM-supplied op-level override's reject-not-clamp check
# (op_runtime/sandboxed_exec.py) both read the RESOLVED SandboxPolicy
# fields, not these two constants directly (these two are only the
# dataclass defaults). The exec tool's SCHEMA TEXT (tools/descriptions/
# execution.py) deliberately carries no number at all (lead-coder ruling,
# option (b)) — it does not read these constants or the resolved policy;
# exec.py has no schema_enricher (that mechanism only reaches tools on the
# router_tools.build_tools() ToolSpec path, not universal_catalog's
# describe path exec uses — dynamic per-session schema injection, option
# (a), was considered and explicitly deferred, not implemented).
DEFAULT_EXEC_TIMEOUT_SECONDS: int = 120
DEFAULT_MAX_EXEC_TIMEOUT_SECONDS: int = 600

# #3903 a-2 (owner ruling 2026-08-11, on top of #3903① above): foreground and
# background exec are DIFFERENT resource envelopes and get their OWN default
# + ceiling — 4 values total, not the single shared field #3903's own issue
# body named as the problem ("一時セッション内でも同じ 60 秒 ── 待たないは
# 移るが上限は移らない"). architect's engineering finding (issue #3903
# comment, 2026-08-11) is why this stays a wall-clock cap rather than being
# dropped in favor of cost/budget bounding: `CostConfig`'s every axis measures
# tokens/dollars (`record(tokens=, cost_usd=)` = LLM usage), never wall-clock
# time, so a subprocess that emits no LLM calls (`exec("sleep 86400")`) never
# moves the budget needle — the checkpoint would never fire regardless of how
# often it's evaluated. Removing the wall-clock cap here would not "let
# budget bound it instead"; it would remove the only instrument that bounds
# THIS resource (process wall-clock time is not tokens or dollars).
#
# DEFAULT_BACKGROUND_EXEC_TIMEOUT_SECONDS is a judgment call (tui-coder,
# 2026-08-11), not a measured or owner-specified number — the same posture
# #4179's own 120/600 took ("realizing a spec, not something a measurement
# should block"), scaled up for the background case: 30 minutes is long
# enough to cover the class of background work #3903's own reference section
# describes (a build step, a long-running install) without being unbounded.
# Open to revision by whoever picks the actual number next; the type/shape
# (a real int, operator-settable, distinct from foreground) is the owner
# ruling, this magnitude is not.
DEFAULT_BACKGROUND_EXEC_TIMEOUT_SECONDS: int = 1800
# owner ruling (2026-08-11, relayed via lead-coder): the background CEILING's
# default is None (no cap) — NOT a large sentinel int. A sentinel (e.g.
# 2**31-1) would still be a real number an LLM-supplied override could be
# checked against and rejected for exceeding, which is meaningless when the
# intent is "no ceiling by default"; `None` lets the reject-not-clamp check
# below (`sandboxed_exec.py`) skip the ceiling comparison entirely rather
# than compare against an arbitrary large number that means nothing.
DEFAULT_BACKGROUND_MAX_EXEC_TIMEOUT_SECONDS: "int | None" = None


@dataclass
class SandboxPolicy:
    """Declarative sandbox policy. See module docstring for field semantics.

    #3901 PR-B ④: every axis's DEFAULT is now compat (owner ruling B,
    2026-08-09 — "seatbelt for a shell that already has the keys" is not the
    model; the sandbox's job is bounding what happens BEHIND a permitted
    action, not re-deciding what the launching shell could already do).
    ``network`` already was compat by owner decision 2026-06-05 (#3905
    aligned its declared default with that decision); #3901 extends the
    same posture to every other axis below.
    Fields are named as DENY-LISTS throughout (restrict-only, #1199) rather
    than allow-lists, because a compat-default axis's natural expression is
    "empty = nothing extra denied" — an allow-list's empty state is
    ambiguous between "nothing permitted" and "no restriction declared"
    (#3899's `write_paths`/`SandboxLayer` two-machines-one-field defect was
    exactly this ambiguity, at the layer this dataclass feeds). The one
    exception is ``write_paths``, kept as an allow-list DELIBERATELY: it is
    the operator-unknowable value (#3901 §1: "this op needs this directory"
    — the operator cannot express what they don't know) the kernel backend
    (Seatbelt/Landlock) consumes directly to build the actual confinement
    rule; #3901 PR-B ③ already removed it from the permission ∩ (it no
    longer double-duties as a permission-narrowing value), so its allow-list
    shape here does not reintroduce the ambiguity that broke.
    """

    # ``DEFAULT_SANDBOX_NETWORK`` (above) is the single source both this
    # dataclass default and the agent-level ``resolve_sandbox_policy`` floor
    # read from (owner decision 2026-06-05, #3905 aligned the two after they
    # drifted). #3901 PR-B ④ generalises that same compat-by-default posture
    # to every other axis below.
    network: bool = DEFAULT_SANDBOX_NETWORK
    # #3901 PR-B ④: read_paths removed — #1199's broad-read realignment
    # already made every kernel backend ignore it (Seatbelt: unconditional
    # ``(allow file-read*)``; Landlock: reads are never gated), and PR-B ③
    # removed the sole remaining consumer (SandboxLayer's permission-∩
    # projection). A field nothing reads is worse than no field: it invites
    # an operator to believe writing it does something.
    write_paths: list[str] = field(default_factory=list)
    # #3901 PR-B ④ (owner ruling B): compat default — was
    # ``DEFAULT_SENSITIVE_READ_DENY``, now empty. #3202's credential-axis
    # carve-out ("read_deny_paths / network stay deny-by-default because
    # they gate secret exposure, not developer UX") is EXPLICITLY
    # overridden by the owner's full-compat ruling on #3901 (network's
    # default flips to open, env_passthrough — see below — to permissive);
    # read_deny_paths follows the same ruling rather than being a
    # remaining island of the old policy. An operator who wants the old
    # credential-path defense-in-depth sets ``read_deny_paths`` explicitly
    # (or the presets #3901 leaves for a future ``sandbox.mode`` — #3823).
    read_deny_paths: list[str] = field(default_factory=list)
    # #3901 PR-B ④: new. The write-axis's own deny-list, mirroring
    # ``read_deny_paths`` — Seatbelt previously derived a write-deny
    # side-effect FROM ``read_deny_paths`` (seatbelt.py; an accident #3901
    # promotes to an explicit, cross-backend-consistent axis) and Landlock
    # never enforced it at all (a same-policy-different-meaning-per-OS gap
    # #3901 closes by giving both backends one real field to read).
    write_deny_paths: list[str] = field(default_factory=list)
    # #3901 PR-B ④: renamed from ``allow_subprocess`` (was: True = compat).
    # Same compat-by-default semantics, restated as a deny-list-shaped bool
    # for consistency with every other axis's post-#3901 vocabulary — an
    # explicit ``deny_subprocess=True`` at any call site still wins (#2964's
    # explicit-beats-floor rule is unaffected by the rename).
    deny_subprocess: bool = False
    # #3901 PR-B ④ (owner ruling B): renamed from ``env_passthrough``
    # (an ALLOW-list, empty = pass nothing but the standard network set —
    # see the former ``resolve_passthrough_env`` docstring). Now a
    # DENY-list, empty = pass everything (full compat: a child inherits the
    # same environment — API keys included — the launching shell already
    # had). This is a real behavior change, not a rename: before this
    # field existed, ``OPENAI_API_KEY``-shaped vars did NOT reach a
    # sandboxed child; after, they do by default. An operator who wants
    # the old narrow passthrough sets ``env_deny_names`` to block specific
    # names, or (#3823, future) selects a stricter ``sandbox.mode``.
    env_deny_names: list[str] = field(default_factory=list)
    # #3823: new — a genuine allow-list capability, not a rename. A
    # deny-list-only env axis cannot express "nothing except these N names"
    # without enumerating the entire universe of possible env vars, which
    # `sandbox.mode: strict` needs to say "pass nothing through by default".
    # ``None`` (default) keeps the deny-list-only behavior above unchanged;
    # see :func:`resolve_passthrough_env`.
    allow_env_names: "list[str] | None" = None
    # #3903①: owner ruling 2026-08-11 — 60 -> 120, matching industry
    # foreground precedent (Claude Code: 120s default). Applied when the LLM's
    # `exec` call omits `timeout` (SandboxedExecIROp.timeout_seconds is None).
    timeout_seconds: int = DEFAULT_EXEC_TIMEOUT_SECONDS
    # #3903①: the ceiling `SandboxedExecIROp.timeout_seconds` (the LLM's
    # optional per-call override) is checked against — architect ruling: an
    # OPERATOR-settable ceiling, not a hardcoded 600, so the LLM can never
    # widen an operator's own configured envelope. See
    # DEFAULT_MAX_EXEC_TIMEOUT_SECONDS's docstring above.
    max_timeout_seconds: int = DEFAULT_MAX_EXEC_TIMEOUT_SECONDS
    # #3903 a-2 (owner ruling 2026-08-11): background exec's OWN default —
    # see DEFAULT_BACKGROUND_EXEC_TIMEOUT_SECONDS's docstring above for why
    # this is a distinct field rather than reusing `timeout_seconds` (the
    # single-shared-field shape #3903's own issue body identified as the
    # problem). Applied when a background exec omits its own timeout, the
    # same relationship `timeout_seconds` has to a foreground call.
    background_timeout_seconds: int = DEFAULT_BACKGROUND_EXEC_TIMEOUT_SECONDS
    # #3903 a-2: background exec's OWN ceiling, `int | None` — `None` (the
    # default) means no ceiling is enforced, distinct from `max_timeout_seconds`
    # (foreground) which stays a real int. See
    # DEFAULT_BACKGROUND_MAX_EXEC_TIMEOUT_SECONDS's docstring above for why
    # `None` rather than a sentinel large int.
    background_max_timeout_seconds: "int | None" = DEFAULT_BACKGROUND_MAX_EXEC_TIMEOUT_SECONDS
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES


def deny_narrowed_write_grants(policy: SandboxPolicy) -> list[tuple[str, str]]:
    """Return ``(write_path, deny_path)`` pairs where a ``write_deny_paths`` entry
    overlaps a ``write_paths`` grant — i.e. where the deny-always-wins rule
    (#2978) actually NARROWS a grant the operator/caller declared.

    #3901 PR-B ④: checks ``write_deny_paths`` (the write axis's OWN deny-list),
    not ``read_deny_paths``. Before PR-B, Seatbelt derived a write-deny
    side-effect FROM ``read_deny_paths`` (denying a credential path's read
    happened to also deny writing to it) while Landlock never enforced that
    side-effect at all — the same policy meant different things per OS. PR-B
    gives both backends one real, explicit field to read for write-denial;
    this function follows that field, not the accident it replaces.

    Pure function of the policy (no I/O, no events) so it is trivially testable
    and can be called from any layer that has an events sink. The op handler
    uses it to emit a ``sandbox_policy_narrowed`` audit-event so a narrowing is
    never silent — the owner requirement that a deny winning over a write grant
    is observable, not a silent drop.

    Overlap = either path contains the other, matching the SBPL ``subpath``
    semantics the Seatbelt backend enforces (a deny on ``~/.ssh`` narrows a
    write grant on ``~``; a deny on ``~/.ssh`` also fully nullifies an explicit
    write grant on ``~/.ssh/x`` — both are reported so the operator can widen
    ``write_deny_paths`` if the write was intended). Paths are ``~``-expanded and
    resolved to match what the backend compares.
    """
    writes = [
        (raw, expand_policy_path(raw).resolve(strict=False))
        for raw in policy.write_paths
    ]
    denies = [
        (raw, expand_policy_path(raw).resolve(strict=False))
        for raw in policy.write_deny_paths
    ]
    narrowed: list[tuple[str, str]] = []
    for w_raw, w in writes:
        for d_raw, d in denies:
            if w == d or w.is_relative_to(d) or d.is_relative_to(w):
                narrowed.append((w_raw, d_raw))
    return narrowed


#: Backend names that cannot express a read/write deny-list at all (#3901 §4③):
#: Landlock is allowlist-only (LSM path-beneath grants; you cannot carve a
#: subpath out of an allowed parent — landlock.py's own module docstring),
#: so a configured ``read_deny_paths``/``write_deny_paths`` is silently
#: unenforced there, unlike Seatbelt's deny-after-allow SBPL rules. This is a
#: structural backend limitation, not a bug to fix — see the module docstring.
#:
#: #3951: this frozenset — and everything ``unenforced_axes()`` below reports
#: — covers ONLY the deny-list-incapable-backend gap. It does NOT report a
#: backend that simply does not enforce an axis at all (e.g. the Docker
#: launch backend currently enforces none of write_paths/network/subprocess
#: — measured, #3823 co-vet). A caller reading a clean ``unenforced_axes()``
#: result must not read it as "this backend enforces everything configured."
_DENY_LIST_INCAPABLE_BACKENDS: frozenset[str] = frozenset({"landlock"})


def unenforced_axes(backend_name: str, policy: SandboxPolicy) -> list[str]:
    """Return the policy axis names *configured* but *structurally unenforceable*
    on ``backend_name`` — the visibility mechanism #3901 §4③ names for a backend
    capability gap that cannot be fixed (Landlock's LSM constraint), only made
    observable.

    Pure function of the policy + backend name (no I/O, no events) — same shape
    as :func:`deny_narrowed_write_grants` — so a caller with an events sink emits
    a ``sandbox_axis_unenforced`` audit-event when this is non-empty. Landlock is
    the only backend in this set today; Seatbelt enforces both deny-lists via
    SBPL deny-after-allow, so it never appears here. Deliberately NOT wired into
    ``enforcement_self_test`` (CLAUDE.md hard rule: that function is the
    PRODUCTION gate, blast radius every sandboxed op on every host, deny-leg ×
    write/spawn axes only — this is audit visibility for a DECLARED gap, not a
    self-test probe).

    #3951: an empty return is NOT a claim that ``backend_name`` enforces every
    configured axis — it only means this backend isn't in
    :data:`_DENY_LIST_INCAPABLE_BACKENDS`. A backend that enforces nothing at
    all for a given axis (the Docker launch backend today, for
    write_paths/network/subprocess) also returns ``[]`` here, silently.
    """
    if backend_name not in _DENY_LIST_INCAPABLE_BACKENDS:
        return []
    axes: list[str] = []
    if policy.read_deny_paths:
        axes.append("read_deny_paths")
    if policy.write_deny_paths:
        axes.append("write_deny_paths")
    return axes


#: #3823: per-backend prose explaining WHY an axis is unenforced there — the
#: same 2-value classification (enforced/unenforced) architect and lead-coder
#: settled on (owner: "reyn が allow_env_names を書けると公開している以上,
#: Docker で効かないのは backend が強制できない — Landlock の deny と同じ状態"),
#: only the WORDING differs by backend/reason. Landlock is the only backend
#: this fires for today (Seatbelt enforces both deny-lists via SBPL
#: deny-after-allow); the dict stays keyed by backend name (not hardcoded to
#: one message) so a future backend with a DIFFERENT reason for the same
#: unenforced state (e.g. a container backend whose image, not reyn, decides
#: what env reaches the process) gets its own prose without a new axis value.
_UNENFORCED_AXIS_REASONS: dict[str, str] = {
    "landlock": (
        "this backend cannot express a deny-list here — Landlock is an LSM "
        "allowlist-only constraint (you cannot carve a subpath out of an "
        "allowed parent)"
    ),
}


def unenforced_axis_reason(backend_name: str) -> str:
    """Human-readable reason *backend_name* cannot enforce an axis
    :func:`unenforced_axes` names — for the audit-event payload and the
    paired WARN log line (#3823). Falls back to a generic statement for a
    backend not in :data:`_UNENFORCED_AXIS_REASONS` (defensive; every backend
    :func:`unenforced_axes` currently classifies as incapable has an entry)."""
    return _UNENFORCED_AXIS_REASONS.get(
        backend_name,
        f"this backend ({backend_name!r}) cannot enforce this axis",
    )


#: #3823: an operator-facing ``sandbox.policy`` CONFIG-vocabulary key →
#: the internal ``SandboxPolicy`` field name it resolves to. This is the
#: decoupling point: the config vocabulary (``<direction>_<axis>_<unit>`` /
#: bare bool axis, tool-naming.md R1 word order) and ``SandboxPolicy``'s own
#: dataclass field names/senses are DIFFERENT vocabularies now, translated
#: explicitly here rather than the same vocabulary transcribed 1:1 via
#: ``**`` unpacking (the #3901 PR-B ④ shape this supersedes) — see
#: :func:`_translate_sandbox_policy_config`. Only ``"subprocess"`` diverges
#: in SENSE (inverted) as well as name; every other key is a pure rename.
_SANDBOX_POLICY_CONFIG_KEY_TO_FIELD: dict[str, str] = {
    "network": "network",
    "subprocess": "deny_subprocess",  # value inverts — see _translate below
    "allow_write_paths": "write_paths",
    "deny_write_paths": "write_deny_paths",
    "deny_read_paths": "read_deny_paths",
    "allow_env_names": "allow_env_names",
    "deny_env_names": "env_deny_names",
    "timeout_seconds": "timeout_seconds",
    "max_timeout_seconds": "max_timeout_seconds",
    # #3903 a-2: background exec's own pair, pure renames like the
    # foreground pair above — the fg/bg distinction lives in the FIELD
    # name, not a translated sense.
    "background_timeout_seconds": "background_timeout_seconds",
    "background_max_timeout_seconds": "background_max_timeout_seconds",
    "max_output_bytes": "max_output_bytes",
}

#: The full config-vocabulary key set — the allowlist
#: :func:`_translate_sandbox_policy_config` fails loudly against.
_SANDBOX_POLICY_CONFIG_KEYS: frozenset[str] = frozenset(
    _SANDBOX_POLICY_CONFIG_KEY_TO_FIELD
)


def unknown_sandbox_policy_config_keys(config_policy: "dict | None") -> frozenset[str]:
    """Return the subset of *config_policy*'s keys that are not in the
    ``sandbox.policy`` config vocabulary (:data:`_SANDBOX_POLICY_CONFIG_KEYS`).

    The one detection primitive both :func:`_translate_sandbox_policy_config`
    (silent-filter, see its docstring for why it no longer raises) and the
    #4174 T0 unified unknown-key WARN mechanism (registered onto
    ``config_schema`` from ``reyn.config.infra`` — this module does not
    import ``config_schema`` itself, preserving the leaf's dependency
    direction) read from — never two independently-maintained "is this key
    known" checks.
    """
    if not isinstance(config_policy, dict):
        return frozenset()
    return frozenset(config_policy) - _SANDBOX_POLICY_CONFIG_KEYS


def _translate_sandbox_policy_config(config_policy: "dict | None") -> dict:
    """Translate an operator-written ``sandbox.policy`` dict (config
    vocabulary: ``allow_write_paths`` / ``deny_read_paths`` /
    ``deny_write_paths`` / ``subprocess`` / ``allow_env_names`` /
    ``deny_env_names`` / ``network`` / ``timeout_seconds`` /
    ``max_timeout_seconds`` / ``max_output_bytes`` — #3823's
    ``<direction>_<axis>_<unit>`` word order,
    a bool axis using the bare axis name) into ``SandboxPolicy`` constructor
    kwargs (the #3916 internal deny-vocabulary shape, unchanged).

    #4174 T0 (owner ruling — "no hard-fail anywhere, don't special-case
    sandbox.policy"): an unknown/misspelled key is now SILENTLY FILTERED
    here, not raised — this supersedes #3823's original "an operator typo
    must fail LOUDLY" posture. The loud half of that promise now happens
    earlier and non-fatally: at config-load / hot-reload / ``reyn config
    validate`` time (:func:`reyn.config.config_schema.unknown_config_keys`,
    fed by :func:`unknown_sandbox_policy_config_keys` via a registered
    freeform-leaf validator), which names ALL unknown keys across the WHOLE
    config in one pass and reports the effective resolved policy alongside
    — the loosening-risk this axis carries (dropping a policy key makes the
    config LOOSER, not silently inert, unlike an ordinary unknown key
    elsewhere). By the time this function runs (every
    ``resolve_sandbox_policy`` call, i.e. every sandboxed op), that warning
    has already had its chance to surface; re-raising here would crash a
    live op on a key that was already reported, not newly discovered — the
    exact "an already-warned condition takes the session down anyway" gap
    the OS-wide warn-not-fail posture (#4174) exists to close. See
    ``SandboxConfig.__post_init__`` for the construction-time side of the
    same change.
    """
    if config_policy is None:
        return {}
    unknown = unknown_sandbox_policy_config_keys(config_policy)
    out: dict = {}
    for key, value in config_policy.items():
        if key in unknown:
            continue
        field_name = _SANDBOX_POLICY_CONFIG_KEY_TO_FIELD[key]
        if key == "subprocess":
            out[field_name] = not value
        else:
            out[field_name] = value

    # #3903① / #4174 T0 (architect flag — folding this into the same
    # warn-not-fail scope so it isn't the one hard-fail left standing once
    # the unknown-key raise above is removed): timeout_seconds/
    # max_timeout_seconds is a self-consistency check, not a hardcoded
    # ceiling — an operator-declared default ABOVE their own configured max
    # is a config error, but per the owner's blanket "no hard-fail
    # anywhere" ruling this now WARNS and clamps the effective default down
    # to the max, rather than crashing every sandboxed op on this
    # deployment. Clamping (not silently keeping the over-max value) is the
    # loosening-risk-safe direction — the operator's own ceiling still
    # wins.
    effective_timeout = out.get("timeout_seconds", DEFAULT_EXEC_TIMEOUT_SECONDS)
    effective_max = out.get("max_timeout_seconds", DEFAULT_MAX_EXEC_TIMEOUT_SECONDS)
    if effective_timeout > effective_max:
        _log.warning(
            "sandbox.policy.timeout_seconds (%s) exceeds "
            "sandbox.policy.max_timeout_seconds (%s) — the default cannot "
            "be above the LLM-extensible cap; using %s as the effective "
            "default instead of failing this op",
            effective_timeout, effective_max, effective_max,
        )
        out["timeout_seconds"] = effective_max

    # #3903 a-2: same default<=max invariant, for the background pair. The
    # ceiling here is `int | None` (None = no ceiling, owner ruling) —
    # skip the comparison when it's None, there is nothing to exceed.
    effective_bg_timeout = out.get(
        "background_timeout_seconds", DEFAULT_BACKGROUND_EXEC_TIMEOUT_SECONDS
    )
    effective_bg_max = out.get(
        "background_max_timeout_seconds", DEFAULT_BACKGROUND_MAX_EXEC_TIMEOUT_SECONDS
    )
    if effective_bg_max is not None and effective_bg_timeout > effective_bg_max:
        raise ValueError(
            f"sandbox.policy.background_timeout_seconds ({effective_bg_timeout}) "
            f"exceeds sandbox.policy.background_max_timeout_seconds "
            f"({effective_bg_max}) — the default cannot be above the "
            f"LLM-extensible cap"
        )
    return out


#: #3823 ①: the per-axis DEFAULT ``sandbox.mode: strict`` applies for a
#: config-vocabulary key the operator left unset. Keyed by INTERNAL
#: ``SandboxPolicy`` field name (the same vocabulary :func:`resolve_sandbox_policy`
#: merges onto) — deliberately excludes ``write_paths``: write's default is
#: the caller-supplied workspace floor regardless of mode (an
#: operator-unknowable per-op value, #3901 PR-B ①②; lead-coder's correction —
#: "strict = nothing allowed" is right for network/subprocess/env, wrong for
#: write, which would also block writing to the op's own workspace). ``read``
#: has no mode-based default at all — there is no ``allow_read_paths``
#: concept (#1199 removed it entirely); only an explicit ``deny_read_paths``
#: narrows either mode.
_SANDBOX_STRICT_MODE_DEFAULTS: dict[str, object] = {
    "network": False,
    "deny_subprocess": True,
    "allow_env_names": [],
}


# ── default sandbox policy resolution (#1339 / sandbox-model completion) ──────
#
def resolve_sandbox_policy(
    config_policy: dict | None,
    *,
    write_paths: list[str] | None = None,
    mode: str = "compat",
) -> dict:
    """Resolve the effective agent-level sandbox policy as a dict.

    The concrete DEFAULT is a **floor** (never None) so the op_runtime handler
    always applies an operator-or-default policy and the LLM-supplied op fields
    are never used as the sandbox policy (closes #1339). The floor = ``network``
    from :data:`DEFAULT_SANDBOX_NETWORK` + ``write_paths`` tight to the
    workspace (the caller-supplied ``write_paths`` = "this op needs this
    directory", a value the operator cannot know — #3901 PR-B ①②: this stays
    a SandboxPolicy floor rather than a permission value for exactly that
    reason).

    #3823 ①: ``mode`` decides the default for a config-vocabulary key the
    operator left UNSET — ``'compat'`` (default) leaves every axis at
    ``SandboxPolicy``'s own compat dataclass default; ``'strict'`` applies
    :data:`_SANDBOX_STRICT_MODE_DEFAULTS` for every key the operator did NOT
    explicitly write. Either way, an explicit operator write ALWAYS wins over
    the mode-derived default — mode never second-guesses an explicit
    ``allow_X``/``deny_X`` or bare-bool write (#2964's explicit-beats-floor
    rule, now generalised to mode as well as the caller floor).

    An operator-declared ``reyn.yaml sandbox.policy`` mapping (config
    vocabulary — #3823) is **merged onto the floor**, not substituted
    wholesale (#2964). Only the fields the operator actually wrote override
    the floor; fields they omitted keep the floor value (or the mode-derived
    default) — so writing ``subprocess: false`` alone no longer silently
    drops the caller's ``write_paths`` (workspace write access). This is the
    owner design principle: *the default is the floor an operator ADDS to;
    only an explicit write is the operator's expressed will.*

    "Wrote it" is expressed by dict-key presence: ``allow_write_paths: []`` is
    an explicit empty grant (respected — the caller's write_paths are
    overridden by the operator's deliberate empty list), whereas OMITTING
    ``allow_write_paths`` keeps the floor's caller-supplied value. dict
    semantics make the "explicit-empty vs omitted" distinction the whole fix
    hinges on directly representable — no separate sentinel is needed.
    """
    floor: dict = {
        "network": DEFAULT_SANDBOX_NETWORK,
        "write_paths": list(write_paths or []),
    }
    explicit = _translate_sandbox_policy_config(config_policy)
    if mode == "strict":
        for key, value in _SANDBOX_STRICT_MODE_DEFAULTS.items():
            if key not in explicit:
                floor[key] = value
    floor.update(explicit)
    return floor
