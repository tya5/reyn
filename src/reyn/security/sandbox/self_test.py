"""Enforcement self-test — the observation that turns "the mechanism is present"
into "the mechanism fired a deny" (#2983 stage 1).

Every sandbox layer reyn ships was found non-functional in a single night — the
seccomp filter had never loaded (#2962), the Landlock shim was unreachable under
the pinned package (#2980), and the Seatbelt deny-list was silently overridden by
a write grant (#2978) — and ``available()`` returned True through all three. It
could only ever answer *is the mechanism present*: right OS, package imports,
kernel ABI >= 1. A backend whose enforcement is dead answers every one of those
questions correctly, so ``sandbox.on_unsupported`` (the operator's fail-closed
knob) never fired: the backend kept claiming an enforcement it had not once
delivered, and even an operator who set ``error`` could not find out. This module
supplies the missing observation, so that claim is checked on the host that makes
it rather than asserted from a checkout that cannot witness it.

**What it probes, and why that specific deny.** It attempts a write to a path
OUTSIDE ``write_paths`` and requires that the write not happen. That is the only
deny both real backends can express: Landlock is allowlist-only and structurally
CANNOT carve a read deny out of an allowed parent (``read_deny_paths`` is
documented as unenforceable there), so probing a ``read_deny_paths`` read — the
first shape considered — would fail on a perfectly healthy Landlock and disable
it everywhere via ``on_unsupported``. A write outside the grant, by contrast, is
denied by construction on both: Seatbelt starts from ``(deny default)``, Landlock
governs the full write surface and grants only ``write_paths``.

**The probe runs against a fixed synthetic policy, never the operator's.** Two
temp dirs, one granted and one not, make the expected deny a property of this
module rather than of whatever the operator happened to configure — so there is
no "what if ``read_deny_paths`` is empty" branch, and no way for a wide policy
(``write_paths: ["/"]``, the example #2978 found in our own docs) to leave the
probe with nothing to prove.

**A one-sided probe would be decoration.** If the wrapped command never ran at
all, "the file was not created" is equally true, and a self-test that reports
success because nothing happened is exactly the failure mode this issue exists to
close. So the probe is two-sided: first a POSITIVE CONTROL that a write to the
GRANTED path does happen, and only then the denied write. Enforcement is
witnessed only when the control succeeds AND the escape is denied; a control that
fails reports the backend as unwitnessed rather than passing it. The oracle is
the filesystem — whether the file exists — not the exit code, because the file is
the security property and the exit code is only a backend's report of it.

**Two axes, two probes, because one policy cannot express both (#2983 stage 2).**
The write probe must set ``allow_subprocess=True`` to isolate its axis from the
syscall layer; the subprocess probe must set it to ``False``, because that flag
IS its subject. The policies contradict, so a single launch cannot witness both
and :func:`probe_subprocess_enforcement` is a second probe rather than another
assertion inside the first. It matters that it exists at all: the write boundary
is Landlock's (or Seatbelt's file rules), so a host where the seccomp filter
never loads — #2962, exactly — passes the write probe green. Until stage 2 a
passing ``available()`` said nothing whatsoever about ``allow_subprocess``, while
``configure-sandbox.md`` told operators it was enforced.

**A third probe exists, added for #3030, but is deliberately NOT folded into
the cached suite below.** :func:`probe_network_enforcement` witnesses the
network gate the same way — a socket-create attempted under ``network=False``
MUST be refused, with the same positive-control / non-networking-control /
deny three-arm shape as the subprocess probe. It closes the gap #3030 found:
the network gate lived inside the SAME seccomp filter the subprocess axis does,
and that filter used to be skipped entirely whenever ``allow_subprocess`` was
True — the stdio-MCP default — so neither ``probe_enforcement`` nor
``probe_subprocess_enforcement`` (both of which pass ``allow_subprocess=True``
to isolate their own axis) had ever exercised it.

It stays OUT of :func:`enforcement_self_test` — the function every real backend
resolution calls — on purpose: that function's blast radius is every
sandboxed op on every host, and a probe bug (a timeout, a host where bare
``socket()`` creation is itself blocked by something unrelated to this fix)
would silently fall every op back to ``NoopBackend`` rather than only failing
to witness one axis. ``probe_network_enforcement`` is instead a directly
callable, uncached probe — the same shape ``sandbox_landlock_deny_gate.py``
(#2983 stage 3) already uses for the other two axes as CI-only deny arms, not
production gates. Widening the cached, production-gating suite to a third axis
is a follow-up decision, not this fix's.

**Known blind spot (two denies in the cached suite, a third probe alongside
it).** :func:`enforcement_self_test` witnesses the filesystem write boundary
and the subprocess-spawn deny, both through ``wrap_command``. It does NOT
witness the network gate (covered by the separate, uncached
:func:`probe_network_enforcement`), ``read_deny_paths`` (not expressible on
Landlock at all), io_uring specifically (covered instead by
``tests/test_sandbox_seccomp_network_3030.py``'s dedicated probe, since it needs
a raw-syscall oracle rather than a marker file), or the one-shot ``run()``
path's separate preexec ruleset — which loads its filter through a DIFFERENT
code path than the shim ``wrap_command`` re-execs, so passing here does not
speak for it. A backend that passes the cached suite has fired two denies —
strictly more than the zero any environment witnessed before #2983 — not every
deny it claims.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .policy import SandboxPolicy

if TYPE_CHECKING:
    from .backend import SandboxBackend

_logger = logging.getLogger(__name__)

# Wall-clock cap for a single probe launch. Generous: the Landlock wrap re-execs
# a fresh interpreter (the shim), so this is not the "a touch takes 5ms" budget.
# It exists so a wedged wrap degrades to a reported failure instead of hanging
# backend resolution forever.
_PROBE_TIMEOUT_SECONDS = 30

# Process-global, keyed by backend name. The probe measures a property of the
# HOST (kernel, installed package, wrapper binary), not of a backend instance,
# and `get_default_backend()` builds a FRESH instance per call — including from
# `sandboxed_exec` on every op. An instance-scoped cache (what `available()`
# uses) would therefore re-spawn probe subprocesses on every sandboxed op, and a
# BROKEN backend would pay that cost forever. Failures are cached for exactly
# that reason: a backend that cannot enforce must not become a per-op subprocess
# tax. The cost of the cache is that a host changing under a live process (a
# package installed mid-run) is not noticed until restart — the same bound
# `seccomp.is_available()`'s module-level cache already accepts.
_CACHE: dict[str, str | None] = {}


def _reset_cache_for_tests() -> None:
    """Test hook: drop the process-global probe cache."""
    _CACHE.clear()


def _attempt_create(
    backend: "SandboxBackend",
    policy: SandboxPolicy,
    target: Path,
    argv: list[str],
) -> tuple[bool, str]:
    """Try to create *target* by running *argv* through *backend*'s own wrap.

    Returns ``(created, detail)`` where ``created`` is read from the FILESYSTEM
    (not the exit code — the file is the security property; the exit code is only
    the backend's report of it) and ``detail`` describes what was observed, for
    an operator-facing message.

    Goes through ``wrap_command`` deliberately: that is the real command-level
    launch seam (MCP stdio / CodeAct), so a wrap that is broken end-to-end — the
    #2980 shape, where the shim raises before it ever restricts anything — is
    caught here rather than reported as healthy.
    """
    try:
        wrapped = backend.wrap_command(list(argv), policy)
    except Exception as exc:  # noqa: BLE001 — any wrap failure is a probe failure
        return False, f"wrap_command() raised {type(exc).__name__}: {exc}"

    try:
        try:
            proc = subprocess.run(  # noqa: S603
                wrapped.argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                check=False,
            )
        except subprocess.TimeoutExpired:
            return target.exists(), f"timed out after {_PROBE_TIMEOUT_SECONDS}s"
        except OSError as exc:
            return target.exists(), f"could not launch the wrapped command: {exc}"
    finally:
        # finally, not except: the wrap owns a resource (Seatbelt's temp .sb
        # profile) that must be released on every path, including the timeout
        # and OSError returns above.
        if wrapped.cleanup is not None:
            try:
                wrapped.cleanup()
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                _logger.debug("sandbox self-test: wrap cleanup failed", exc_info=True)

    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    detail = f"rc={proc.returncode}"
    if stderr:
        detail += f", stderr={stderr[:400]!r}"
    return target.exists(), detail


def probe_enforcement(backend: "SandboxBackend") -> str | None:
    """Witness whether *backend* actually denies a write outside ``write_paths``.

    Returns ``None`` when the deny fired (= this backend enforces, on this host,
    right now), or a human-readable reason when it did not. The reason is written
    to be read by an operator in a WARNING or a RuntimeError: it names what was
    attempted, what happened, and what that implies.

    Uncached — :func:`enforcement_self_test` is the cached entry point backends
    call. This one is the raw observation, which is also what makes it directly
    testable against a backend that is known not to enforce.
    """
    touch = shutil.which("touch")
    if touch is None:
        return (
            "no 'touch' binary found on PATH, so the enforcement probe could not "
            "be run — this backend's enforcement is unwitnessed, not confirmed"
        )

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-selftest-granted-")).resolve()
    denied = Path(tempfile.mkdtemp(prefix="reyn-sandbox-selftest-denied-")).resolve()
    try:
        # A fixed synthetic policy (never the operator's): write is granted to
        # `granted` and nowhere else, so a write into `denied` MUST be refused by
        # any backend that enforces at all. network/allow_subprocess are left ON
        # and the deny-list empty so this probe isolates the write axis — it is
        # not a test of those layers, and must not fail because of them.
        policy = SandboxPolicy(
            write_paths=[str(granted)],
            read_deny_paths=[],
            network=True,
            allow_subprocess=True,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )

        # 1. Positive control — prove the probe can observe an ALLOWED write.
        #    Without this, "the file is absent" below could just mean the command
        #    never ran, and the self-test would pass by observing nothing.
        control = granted / "control"
        created, detail = _attempt_create(backend, policy, control, [touch, str(control)])
        if not created:
            return (
                f"the enforcement probe could not establish a positive control: "
                f"writing {control} — a path this policy GRANTS — did not happen "
                f"({detail}). The probe cannot observe this backend at all, so a "
                f"denied write would prove nothing; treating enforcement as "
                f"unwitnessed"
            )

        # 2. The deny — the actual claim under test.
        escape = denied / "escape"
        created, detail = _attempt_create(backend, policy, escape, [touch, str(escape)])
        if created:
            return (
                f"no deny fired: writing {escape} SUCCEEDED even though it is "
                f"outside the policy's only write grant ({granted}) ({detail}). "
                f"The backend reports itself available but does not enforce — it "
                f"would have run AI-generated code with no isolation while "
                f"claiming otherwise"
            )
        return None
    finally:
        shutil.rmtree(granted, ignore_errors=True)
        shutil.rmtree(denied, ignore_errors=True)


def probe_subprocess_enforcement(backend: "SandboxBackend") -> str | None:
    """Witness whether *backend* actually denies process spawning under
    ``allow_subprocess=False`` — the axis ``configure-sandbox.md`` attributes to
    seccomp-BPF on Linux and to ``(deny process-fork)`` on macOS (#2983 stage 2).

    Returns ``None`` when the spawn was refused, else an operator-readable reason.
    Uncached; :func:`enforcement_self_test` is the cached entry point.

    Separate from :func:`probe_enforcement` because the two policies contradict —
    see the module docstring. This is the probe that makes ``available() == True``
    say anything at all about ``allow_subprocess``: the write probe is satisfied
    by Landlock alone, so a host whose seccomp filter never loads passes it.

    **Three launches, because two different lies are available here.** The oracle
    is again the filesystem, never the exit code — measured, not assumed: under a
    loaded filter ``touch`` CREATES the file via ``openat`` and only then takes
    EPERM on ``utimensat``, so it reports failure on a write that happened. An
    exit-code oracle would read that as a deny.

    1. ``allow_subprocess=True`` + a forking command must CREATE its marker —
       the positive control of #3016: if the probe cannot watch a spawn succeed,
       the same marker's absence under a deny proves nothing.
    2. ``allow_subprocess=False`` + a NON-forking command must still create its
       marker. This control is specific to this axis and load-bearing: the
       mechanism under test is a default-deny syscall filter, and the first one
       reyn ever loaded killed ``/bin/echo`` outright (#2962). Without this arm a
       filter that refuses EVERYTHING is indistinguishable from one that refuses
       exactly ``fork`` — both leave no marker — and we would report the broadest
       possible breakage as enforcement.
    3. Only then the deny: ``allow_subprocess=False`` + the forking command must
       NOT create its marker. Arms 2 and 3 differ in nothing but the fork, so the
       absence is attributable to the fork rather than to a wrap that is simply
       dead under this policy.
    """
    sh = shutil.which("sh")
    touch = shutil.which("touch")
    cat = shutil.which("cat")
    if sh is None or touch is None or cat is None:
        return (
            "the subprocess-enforcement probe needs 'sh', 'touch' and 'cat' on "
            "PATH and did not find them all, so it could not be run — this "
            "backend's allow_subprocess enforcement is unwitnessed, not confirmed"
        )

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-selftest-spawn-")).resolve()
    try:
        def _policy(allow_subprocess: bool) -> SandboxPolicy:
            # Fixed and synthetic, like the write probe's: write is GRANTED to
            # `granted` throughout, so nothing here turns on a filesystem deny —
            # the only variable across the three arms is allow_subprocess (and,
            # in arm 2, whether the command forks at all).
            return SandboxPolicy(
                write_paths=[str(granted)],
                read_deny_paths=[],
                network=True,
                allow_subprocess=allow_subprocess,
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )

        def _forking_argv(marker: Path) -> list[str]:
            # A PIPELINE, not a nested `sh -c`: measured on both platforms, a
            # shell asked to run one simple command may exec it in place with no
            # fork at all (macOS /bin/sh does exactly that), which would make the
            # deny arm's empty marker mean "nothing forked", not "the fork was
            # refused". A pipeline forces the shell to fork its left-hand side.
            # `pipe`/`pipe2` are seccomp-baseline (#2962), so under the filter
            # this reaches the fork and is refused THERE — the denial `denial.py`
            # already knows how to classify.
            return [sh, "-c", f"{shlex.quote(touch)} {shlex.quote(str(marker))} "
                              f"| {shlex.quote(cat)}"]

        # 1. Positive control — a spawn this probe is ALLOWED to make happens.
        control = granted / "control-spawn"
        created, detail = _attempt_create(
            backend, _policy(True), control, _forking_argv(control)
        )
        if not created:
            return (
                f"the subprocess-enforcement probe could not establish a positive "
                f"control: a command that forks — permitted here by "
                f"allow_subprocess=True — did not produce {control} ({detail}). "
                f"The probe cannot observe a spawn through this backend at all, so "
                f"a missing marker under allow_subprocess=False would prove "
                f"nothing; treating enforcement as unwitnessed"
            )

        # 2. The deny policy must still be able to run a command at all.
        alive = granted / "control-nofork"
        created, detail = _attempt_create(
            backend, _policy(False), alive, [touch, str(alive)]
        )
        if not created:
            return (
                f"under allow_subprocess=False this backend could not run even a "
                f"NON-forking command: {alive} — a path this policy GRANTS — was "
                f"not written ({detail}). Something in this policy's wrap is "
                f"failing wholesale rather than denying process creation "
                f"specifically, so a denied spawn cannot be attributed to the "
                f"allow_subprocess gate; treating enforcement as unwitnessed"
            )

        # 3. The deny — the actual claim under test.
        spawned = granted / "spawned"
        created, detail = _attempt_create(
            backend, _policy(False), spawned, _forking_argv(spawned)
        )
        if created:
            return (
                f"no subprocess deny fired: a command that must fork to run wrote "
                f"{spawned} even though the policy set allow_subprocess=False "
                f"({detail}). The backend reports allow_subprocess as enforced "
                f"while sandboxed code can still spawn arbitrary processes — the "
                f"syscall layer (seccomp-BPF on Linux, (deny process-fork) on "
                f"macOS) is not active"
            )
        return None
    finally:
        shutil.rmtree(granted, ignore_errors=True)


def probe_network_enforcement(backend: "SandboxBackend") -> str | None:
    """Witness whether *backend* actually denies outbound-socket creation under
    ``network=False`` (#3030).

    Returns ``None`` when the deny fired, else an operator-readable reason.
    Uncached, and — unlike :func:`probe_enforcement` /
    :func:`probe_subprocess_enforcement` — deliberately NOT folded into
    :func:`enforcement_self_test`'s cached, production-gating suite; see that
    function's and the module's docstrings for why. Called directly by CI
    (``scripts/sandbox_landlock_deny_gate.py``'s ``network`` deny arm) and by
    ``tests/test_sandbox_seccomp_network_3030.py``.

    The oracle is ``socket.socket()`` succeeding or raising, marked via a file
    (same idiom as :func:`probe_subprocess_enforcement`) rather than a live
    connect: the seccomp filter refuses the ``socket`` syscall itself, before any
    address is even resolved, so no outbound connectivity is needed to witness
    the deny — and none is risked on a network-restricted host running this
    probe.

    **``allow_subprocess=True`` throughout — deliberately, unlike the write
    probe's isolation choice.** #3030 is specifically the discovery that
    ``network=False`` was silently unenforced whenever ``allow_subprocess`` was
    True (the stdio-MCP default), because the whole seccomp filter — network
    gate included — used to be skipped in that case. Probing with
    ``allow_subprocess=False`` would not exercise the condition that was
    actually broken.

    Three launches, same shape as :func:`probe_subprocess_enforcement` and for
    the same reason — two different lies are available:

    1. ``network=True`` + a socket create must SUCCEED (positive control) — else
       the probe cannot observe a working socket() through this wrap at all.
    2. ``network=False`` + a NON-networking command must still run — else a
       filter that is dead wholesale under this policy (exactly #3030's shape:
       the whole filter skipped, not "network off, everything else on") is
       indistinguishable from one that denies only sockets.
    3. Only then the deny: ``network=False`` + a socket create must NOT
       succeed.
    """
    touch = shutil.which("touch")
    if touch is None:
        return (
            "no 'touch' binary found on PATH, so the network-enforcement probe "
            "could not be run — this backend's network enforcement is "
            "unwitnessed, not confirmed"
        )

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-selftest-net-")).resolve()
    try:
        def _policy(network: bool) -> SandboxPolicy:
            return SandboxPolicy(
                write_paths=[str(granted)],
                read_deny_paths=[],
                network=network,
                allow_subprocess=True,  # the exact #3030 condition
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )

        def _socket_argv(marker: Path) -> list[str]:
            code = (
                "import socket\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                f"open({str(marker)!r}, 'w').close()\n"
            )
            return [sys.executable, "-c", code]

        # 1. Positive control.
        control = granted / "control-socket"
        created, detail = _attempt_create(backend, _policy(True), control, _socket_argv(control))
        if not created:
            return (
                f"the network-enforcement probe could not establish a positive "
                f"control: creating a socket — permitted here by network=True — "
                f"did not produce {control} ({detail}). The probe cannot observe "
                f"a socket through this backend at all, so a missing marker "
                f"under network=False would prove nothing; treating enforcement "
                f"as unwitnessed"
            )

        # 2. The deny policy must still be able to run a command at all.
        alive = granted / "control-nonet"
        created, detail = _attempt_create(backend, _policy(False), alive, [touch, str(alive)])
        if not created:
            return (
                f"under network=False (allow_subprocess=True) this backend "
                f"could not run even a NON-networking command: {alive} — a path "
                f"this policy GRANTS — was not written ({detail}). Something in "
                f"this policy's wrap is failing wholesale rather than denying "
                f"sockets specifically, so a denied socket cannot be attributed "
                f"to the network gate; treating enforcement as unwitnessed"
            )

        # 3. The deny — the actual claim under test.
        escaped = granted / "escaped-socket"
        created, detail = _attempt_create(
            backend, _policy(False), escaped, _socket_argv(escaped)
        )
        if created:
            return (
                f"no network deny fired: creating a socket wrote {escaped} even "
                f"though the policy set network=False ({detail}). The backend "
                f"reports network as enforced while sandboxed code can still "
                f"open outbound sockets — the exact #3030 condition "
                f"(allow_subprocess=True skipped the whole syscall filter, "
                f"network gate included)"
            )
        return None
    finally:
        shutil.rmtree(granted, ignore_errors=True)


def enforcement_self_test(backend: "SandboxBackend") -> str | None:
    """Cached probe suite — ``None`` when *backend* fired a real deny on EVERY
    axis it claims, else the reason one did not.

    Both :func:`probe_enforcement` (filesystem write) and
    :func:`probe_subprocess_enforcement` (process spawn) must pass. A backend
    that enforces one axis and silently ignores the other is exactly what
    ``available()`` must stop meaning "yes" for: reyn documents both, and
    ``sandboxed_exec`` callers set both.

    ``probe_network_enforcement`` (#3030) is deliberately NOT part of this
    cached suite — see the module docstring's "third probe" section for why.

    **Why this is all-or-nothing, and not "keep whichever axis passes".** The
    obvious objection is that failing a Linux host over seccomp discards a
    Landlock write boundary that demonstrably works — trading real protection for
    honesty. It does not, because the axes are independent as CHECKS but not as
    PROTECTION. Landlock has no ``chmod`` right at all and path-based ``truncate``
    is outside its handled set, so with seccomp absent BOTH are ungoverned; what
    refuses them is this filter's default-deny, by omitting them from the
    allowlist (``_EXCLUDED_UNGOVERNABLE`` in ``backends/seccomp.py`` records the
    reasoning, and ``seccomp.py``'s module docstring notes Landlock cannot block
    ``ptrace`` either). Measured on Linux 6.8 with Landlock enforcing and no
    filter: ``open()`` on a file outside ``write_paths`` was refused while
    ``os.truncate()`` on that same file succeeded and emptied it. So
    Landlock-without-seccomp is not a weaker sandbox, it is an incoherent one —
    and "writes are enforced" would be a claim about a boundary that a sandboxed
    process can walk around. Failing closed is the correct outcome, not a cost.

    Note the spawn probe does not itself attempt ``truncate``: it witnesses that
    the filter LOADED, which is the precondition for every deny the allowlist
    expresses by omission.

    Cached process-globally on ``backend.name``; see :data:`_CACHE` for why the
    scope is the process and not the instance, and why failures are cached too.
    """
    name = backend.name
    if name in _CACHE:
        return _CACHE[name]
    reason = probe_enforcement(backend) or probe_subprocess_enforcement(backend)
    _CACHE[name] = reason
    if reason is not None:
        _logger.debug("sandbox self-test failed for %r: %s", name, reason)
    return reason
