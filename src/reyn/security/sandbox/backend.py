"""SandboxBackend Protocol + SandboxResult — mechanism abstraction (FP-0017).

The Protocol decouples op handlers from the enforcement mechanism. Concrete
backends (NoopBackend today; SeatbeltBackend / LandlockBackend in future
waves) implement `available()` for platform detection and `run()` for actual
execution under the declared policy.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import Enum
from typing import Protocol, runtime_checkable

from .policy import SandboxPolicy


class AxisEnforcement(Enum):
    """Whether a backend enforces one ``SandboxPolicy`` axis — #4039.

    Two values, not three: the founding design draft (#4039, architect)
    considered a third ``NOT_APPLICABLE`` for "this axis has no meaning on
    this backend" but found no real instance across the 4 current backends
    (noop/seatbelt/landlock/docker) × 7 axes — every cell is a definite
    ENFORCES or DOES_NOT_ENFORCE. A third value withdrawn for lack of a
    driving case is the same call #3823 made for sandbox.mode's "custom"
    value; add it back if a real NOT_APPLICABLE case appears, not
    speculatively.
    """

    ENFORCES = "enforces"
    DOES_NOT_ENFORCE = "does_not_enforce"


@dataclass(frozen=True)
class AxisEnforcementDeclaration:
    """A backend's full per-axis enforcement claim — #4039 (D1/D2).

    Every field is REQUIRED, no defaults anywhere in this class — mirrors
    :class:`~reyn.security.sandbox.axis_contract.AxisContract`'s own
    discipline (that module's docstring: "a new axis registered here must
    say EXPLICITLY whether it has exceptions ... there is no default that
    would let a forgotten field read as 'none'"). A backend that forgets an
    axis fails to CONSTRUCT its declaration — a ``TypeError`` at backend
    module import time, not a silent "not reported" (#4039's own founding
    bug: ``NoopBackend`` enforced nothing yet ``unenforced_axes()`` reported
    nothing, because the OLD predicate's domain was never full).

    Keyed to :class:`~reyn.security.sandbox.policy.SandboxPolicy`'s own
    FIELD names (``deny_subprocess``, not the config-vocabulary
    ``subprocess`` — #3823 deliberately split those two vocabularies;
    ``axis_contract.AXIS_REGISTRY``'s coarser ``write``/``spawn``/``network``
    names are a DIFFERENT, CI-conformance-only vocabulary — see D1/D3 in
    #4039 for why this module does not reuse it), not
    :mod:`~reyn.security.sandbox.axis_contract`'s coarser 3-axis vocabulary —
    the two are separate registries at separate layers (CLAUDE.md's 2-layer
    sandbox rule: this declaration is read by the production predicate
    (:func:`~reyn.security.sandbox.policy.unenforced_axes`), CI conformance
    is a different, CI-only consumer of ``axis_contract``).

    ``allow_env_names`` is declared separately from ``env_deny_names``
    (architect co-vet, #4039) even though every backend today enforces both
    identically (both flow through
    :func:`~reyn.security.sandbox.policy.resolve_passthrough_env`) — they
    are genuinely distinct ``SandboxPolicy`` fields with distinct semantics
    (a deny-list vs. an axis-mode switch to allow-list), and a future
    backend could plausibly diverge on one without the other.
    """

    write_paths: AxisEnforcement
    write_deny_paths: AxisEnforcement
    read_deny_paths: AxisEnforcement
    network: AxisEnforcement
    deny_subprocess: AxisEnforcement
    env_deny_names: AxisEnforcement
    allow_env_names: AxisEnforcement

    def as_dict(self) -> "dict[str, AxisEnforcement]":
        """The declaration as a plain ``{axis_name: AxisEnforcement}`` dict —
        what :func:`~reyn.security.sandbox.policy.unenforced_axes` and CI
        conformance both actually consume."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: Every axis name :class:`AxisEnforcementDeclaration` covers — the full
#: domain D2 requires. Derived from the dataclass's own fields (not a
#: hand-duplicated literal) so this constant can never drift from the type
#: it describes.
SANDBOX_POLICY_AXES: "frozenset[str]" = frozenset(
    f.name for f in fields(AxisEnforcementDeclaration)
)


@dataclass
class WrappedCommand:
    """Result of ``SandboxBackend.wrap_command()`` — a command-level sandbox wrap.

    Command-level wrapping (as opposed to the one-shot ``run()``) is the seam
    for a PERSISTENT subprocess launch that the backend does not itself spawn
    (e.g. a stdio MCP server held open by the caller's transport) — the wrap
    prepends whatever the backend needs (a sandbox-exec invocation, a re-exec
    shim, ...) and hands back everything the caller needs to Popen/exec.

    ``argv`` is the full wrapped argv (wrapper prefix + the original command),
    ready to launch directly. ``env`` is the allowlisted env every ``run()``
    implementation already builds via ``resolve_passthrough_env(policy)`` (+
    the ``PATH`` fallback every backend applies) — added here (#3822) because
    a caller that only reads ``argv`` has no signal that env is a SEPARATE
    thing it must also resolve itself, and two separate persistent-process
    callers (CodeAct #3822, MCP stdio #3848) independently missed exactly
    that and fell back to inheriting the full parent environment. The old
    docstring's "hands the full argv back for the caller to Popen/exec" was
    itself part of the gap: it declared enough for a *safe* exec while
    actually providing only ARGV. ``cleanup``, when set, releases a
    wrap-owned resource (e.g. Seatbelt's temp ``.sb`` profile file) — the
    caller MUST invoke it once the wrapped subprocess is torn down. ``None``
    means the wrap owns no such resource.
    """

    argv: list[str]
    env: dict[str, str]
    cleanup: "Callable[[], None] | None" = None


@dataclass
class SandboxResult:
    """Result of a single sandboxed_exec invocation.

    `truncated` indicates that stdout/stderr were capped by the backend.
    `returncode` is -1 if the process was killed (timeout / signal).
    `cancelled` is True when the run was terminated by cancel_inflight() (#1470).
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    cancelled: bool = False


@runtime_checkable
class SandboxBackend(Protocol):
    """Sandbox backend protocol.

    Implementations declare a `name` attribute (= "noop" / "seatbelt" /
    "landlock" / ...), report platform availability via `available()`, and
    run a command under the supplied policy via `run()`.
    """

    name: str

    #: #4039 (D1/D2): this backend's own per-axis enforcement claim, over
    #: the FULL domain (:data:`SANDBOX_POLICY_AXES`) — no default anywhere
    #: (:class:`AxisEnforcementDeclaration` has none), so a backend that
    #: forgets an axis fails to construct this at module import time. Read
    #: by :func:`~reyn.security.sandbox.policy.unenforced_axes` (production,
    #: declaration-only, never probed) — never by
    #: ``enforcement_self_test`` (CLAUDE.md hard rule: that gate's blast
    #: radius stays narrow, deny-leg only, write+spawn only).
    enforced_axes: AxisEnforcementDeclaration

    def available(self) -> bool:
        """Return True if this backend's enforcement mechanism is PRESENT on the
        current platform (right OS, package imports, kernel ABI).

        Presence is not function: a backend whose enforcement is dead answers
        this correctly (#2962 / #2980 both did). ``self_test()`` is the question
        of whether it WORKS, and `get_default_backend()` asks both before handing
        a backend to a caller.
        """
        ...

    def self_test(self) -> str | None:
        """Return None if this backend actually FIRED a deny on this host, else a
        human-readable reason it did not.

        This is the seam that makes "available" mean "enforcing" (#2983). All
        three sandbox layers were found non-functional while `available()`
        reported True, because presence was the only thing anything ever checked;
        `get_default_backend()` calls this at resolution and applies
        ``sandbox.on_unsupported`` to a non-None result, so a backend that cannot
        enforce is treated exactly like one that is absent — which is what it is.

        Implementations that CLAIM enforcement delegate to
        ``reyn.security.sandbox.self_test.enforcement_self_test(self)`` (a real
        subprocess through this backend's own ``wrap_command``, cached per
        process). NoopBackend is the sole exemption and documents why on its own
        override. There is deliberately no default implementation: a backend that
        forgot to answer must not inherit a silent "yes".
        """
        ...

    def session_artifact_outside_write_scope(self, policy: SandboxPolicy) -> bool:
        """#4434 (stage 1): True iff any on-disk artifact this backend may
        cache/reuse ACROSS calls for *policy* (e.g. Seatbelt's generated SBPL
        ``.sb`` profile) lives outside every write scope *policy* itself
        grants.

        Every backend bears this contract, not just the one that currently
        has something to cache (owner correction, #4434: "the sandbox
        abstraction means every backend needs the abstract contract" — a
        Seatbelt-only precondition would leave a future backend's own
        session-scoped artifact unguarded by construction). A backend that
        produces no such artifact (Landlock re-execs via argv, no cleanup
        resource of its own; NoopBackend wraps nothing) trivially satisfies
        this and returns True unconditionally — there is nothing a
        sandboxed child could rewrite.

        Callers derive the answer from *policy* itself (e.g. resolving
        ``policy.write_paths`` the same way the backend's own enforcement
        does), never from a literal path comparison — the whole point is
        that relocating either side (the cached artifact, or an operator's
        write grant) is caught by construction.
        """
        ...

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Return a command-level sandbox wrap of *argv* for a persistent-process
        launch (e.g. a stdio MCP server) that cannot go through the one-shot
        ``run()``. Every backend implements this uniformly so NO agent-reachable
        command-level launch ever bypasses the abstraction:

        - Seatbelt: prepends ``sandbox-exec -f <profile>`` (a generated SBPL
          profile written to a temp file; the returned ``cleanup`` unlinks it).
        - Landlock: prepends the ``landlock_exec`` re-exec shim argv.
        - NoopBackend: returns *argv* UNCHANGED — passthrough, but the call
          still went THROUGH this method (the owner-acceptable no-enforcement
          case, as opposed to a raw bypass that never consulted the backend).

        Synchronous and side-effect-light (may perform local I/O such as
        writing a temp profile file) — it does not itself spawn the wrapped
        process; the caller owns that.
        """
        ...

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        """Execute argv under the given policy and return the result.

        ``cwd`` is the working directory the command runs in. The OS passes the
        run's ``workspace.base_dir`` (= parity with the legacy ``shell`` op,
        FP-0008 PR-I) so ``git`` / ``pytest`` resolve against the repo root even
        under concurrent benchmark runs. ``None`` = inherit the parent process
        cwd. A workspace-coupled backend (e.g. a container backend whose repo
        lives at an in-container path) may ignore this host-side ``cwd`` and use
        its own baked working directory — same asymmetry as policy enforcement,
        which such a backend also scopes to the fidelity boundary.

        ``cancel_event``: when provided and set, the backend kills the running
        subprocess (SIGTERM → SIGKILL grace) and returns a SandboxResult with
        ``cancelled=True``. None = no cancel-awareness (#1470).
        """
        ...
