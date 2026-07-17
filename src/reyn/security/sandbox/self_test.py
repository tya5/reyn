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

**Known blind spot (stage 1 is one deny, not a matrix).** This witnesses the
filesystem write boundary through ``wrap_command``. It does NOT witness the
network gate, the ``allow_subprocess`` / seccomp syscall layer (the probe policy
sets ``allow_subprocess=True`` precisely to isolate the write axis from it), or
the one-shot ``run()`` path's separate preexec ruleset. A backend that passes has
fired one deny — strictly more than the zero any environment witnessed before
this — not every deny it claims.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
    touch: str,
) -> tuple[bool, str]:
    """Try to create *target* by running ``touch`` through *backend*'s own wrap.

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
        wrapped = backend.wrap_command([touch, str(target)], policy)
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
        created, detail = _attempt_create(backend, policy, control, touch)
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
        created, detail = _attempt_create(backend, policy, escape, touch)
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


def enforcement_self_test(backend: "SandboxBackend") -> str | None:
    """Cached :func:`probe_enforcement` — ``None`` when *backend* fired the deny,
    else the reason it did not.

    Cached process-globally on ``backend.name``; see :data:`_CACHE` for why the
    scope is the process and not the instance, and why failures are cached too.
    """
    name = backend.name
    if name in _CACHE:
        return _CACHE[name]
    reason = probe_enforcement(backend)
    _CACHE[name] = reason
    if reason is not None:
        _logger.debug("sandbox self-test failed for %r: %s", name, reason)
    return reason
