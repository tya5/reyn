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
    timeout_seconds: wall-clock cap (enforced by the backend)
    max_output_bytes: per-stream cap (bytes) on captured stdout/stderr — output
        beyond it is drained-and-discarded (the ``truncated`` flag is set) so a
        flooding child cannot exhaust host memory. Default 10 MiB; overridable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES


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

    PATH fallback is applied by each backend after calling this (preserves the
    existing "PATH always available" behaviour independent of this set).
    """
    deny = set(policy.env_deny_names)
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
    timeout_seconds: int = 60
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
    """
    if backend_name not in _DENY_LIST_INCAPABLE_BACKENDS:
        return []
    axes: list[str] = []
    if policy.read_deny_paths:
        axes.append("read_deny_paths")
    if policy.write_deny_paths:
        axes.append("write_deny_paths")
    return axes


# ── default sandbox policy resolution (#1339 / sandbox-model completion) ──────
#
def resolve_sandbox_policy(
    config_policy: dict | None, *, write_paths: list[str] | None = None
) -> dict:
    """Resolve the effective agent-level sandbox policy as a dict.

    The concrete DEFAULT is a **floor** (never None) so the op_runtime handler
    always applies an operator-or-default policy and the LLM-supplied op fields
    are never used as the sandbox policy (closes #1339). The floor = ``network``
    from :data:`DEFAULT_SANDBOX_NETWORK` + ``write_paths`` tight to the
    workspace (the caller-supplied ``write_paths`` = "this op needs this
    directory", a value the operator cannot know — #3901 PR-B ①②: this stays
    a SandboxPolicy floor rather than a permission value for exactly that
    reason). #3901 PR-B ④ (owner ruling B): every other axis's default is now
    ``SandboxPolicy``'s own dataclass default (compat) — this floor no longer
    overrides ``read_deny_paths`` to :data:`DEFAULT_SENSITIVE_READ_DENY`; an
    operator who wants that defense-in-depth back sets ``read_deny_paths``
    explicitly.

    An operator-declared ``reyn.yaml sandbox.policy`` mapping is **merged onto
    the floor**, not substituted wholesale (#2964). Only the fields the operator
    actually wrote override the floor; fields they omitted keep the floor value
    — so writing ``deny_subprocess: true`` alone no longer silently drops the
    caller's ``write_paths`` (workspace write access). This is the owner design
    principle: *the default is the floor an operator ADDS to; only an explicit
    write is the operator's expressed will.*

    "Wrote it" is expressed by dict-key presence: ``write_paths: []`` is an
    explicit empty grant (respected — the caller's write_paths are overridden by
    the operator's deliberate empty list), whereas OMITTING ``write_paths``
    keeps the floor's caller-supplied value. dict semantics make the
    "explicit-empty vs omitted" distinction the whole fix hinges on directly
    representable — no separate sentinel is needed.
    """
    floor: dict = {
        "network": DEFAULT_SANDBOX_NETWORK,
        "write_paths": list(write_paths or []),
    }
    if config_policy is not None:
        floor.update(config_policy)
    return floor
