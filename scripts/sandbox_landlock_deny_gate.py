"""Standing CI gate: a real Landlock/seccomp deny must FIRE on a real Linux host
(#2983 stage 3).

Stage 1 made ``available()`` mean "fired a deny" and stage 2 added the second
axis, so a user whose sandbox is inert now finds out at backend resolution. That
closes production. It does not close the repo: the only place a Linux deny has
ever been *witnessed* is one maintainer's colima VM, run by hand. Every CI job we
have is blind to it — ``test.yml`` omits the ``sandbox-linux`` extra on purpose,
so ``tests/test_landlock_exec_shim_1344e.py``'s enforcement group skips in all of
them, and a skip is green. A regression that re-breaks enforcement (the #2962
dead-filter / #2980 fictional-Landlock class, both of which lived for weeks under
a green suite) would therefore merge, and be found by a *user's* fail-closed
``self_test()`` rather than at commit time. One host is not a gate. This script is
the gate.

**The invariant is "a deny FIRED", not "the tests passed".** Those come apart, and
this repo has the receipts for every way they do:

  - "the backend reports itself available" is a *different claim* from "the
    backend enforces" — #2980 passed the first for 41 days while failing the
    second, which is the whole reason ``self_test`` exists.
  - "the job was green" is a different claim from "the job ran". A missing extra,
    an ABI-0 kernel, an import that quietly fails: each one turns this gate into
    a skip, and ``rc=0`` reports a skip exactly the way it reports a witness.

So this script has no skip path. Every precondition it needs is a FATAL if
unmet — if the ``sandbox-linux`` extra did not install, that is a broken job, not
an environment to tiptoe around, and it must go red rather than pass having
observed nothing.

**It checks that it is not itself decoration.** The deny arms below prove
enforcement only if the probes behind them are *capable of reporting a failure*.
A ``probe_enforcement`` that returned ``None`` unconditionally would make this
gate green forever, and no assertion about Landlock would notice. So before the
deny arms run, both probes are pointed at ``NoopBackend`` — a real, in-repo,
production backend whose documented contract is "no isolation enforced" (no fake
is needed; we ship one) — and each MUST report a failure there. A probe that
passes ``NoopBackend`` is broken, and this gate fails as vacuous rather than
inheriting a green from it.

**★ ONE ABI. This green does not mean "Landlock is witnessed".** A runner is one
kernel, and a kernel is one Landlock ABI — an older ABI cannot be faked in a
container, because the ABI belongs to the host kernel and not to the image. This
job witnesses whatever ABI GitHub currently ships on ``ubuntu-latest`` (24.04 ≈
ABI 4) and prints it, so the witness names its own scope instead of letting a
green imply a coverage it never had. **ABI 1-2 — Ubuntu 22.04 / RHEL 9 / Debian
12, i.e. most of the installed base, and where #2975's ``FS.TRUNCATE`` gap
actually lives — is NOT covered here** and stays covered only by the runtime
``self_test()`` failing closed on each user's own machine. Do not read a green
run of this script as "the sandbox is validated on Linux". Read it as: "on this
one ABI, on this one kernel, both denies fired today."
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass

# The child source is run as `python -c <source> <probe> <backend>`, so its own
# sys.argv is ["-c", <probe>, <backend>].
#
# Each probe runs in a FRESH subprocess. The probes spawn their own children
# today (the restrictions land in a re-exec'd shim, not here), so nothing would
# poison this process right now — but a seccomp filter is irrevocable for the
# process that loads one, and this gate must not be quietly depending on "the
# probe happens not to load in-process" staying true. Isolating each probe costs
# one interpreter start and removes the dependency. It is also the shape
# `sandbox_seccomp_x86_64_live_smoke.py` already uses, for the same reason.
#
# Imports are explicit, never getattr(): a renamed probe must fail this gate at
# import with an ImportError, loudly. A dynamic lookup with a fallback is how a
# check goes dead without anyone hearing it.
_CHILD_SOURCE = """
import json
import sys

from reyn.security.sandbox import NoopBackend
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.self_test import (
    probe_enforcement,
    probe_subprocess_enforcement,
)

_PROBES = {"write": probe_enforcement, "spawn": probe_subprocess_enforcement}
_BACKENDS = {"landlock": LandlockBackend, "noop": NoopBackend}

reason = _PROBES[sys.argv[1]](_BACKENDS[sys.argv[2]]())
print("REYN-PROBE-RESULT " + json.dumps({"reason": reason}))
"""

_SENTINEL = "REYN-PROBE-RESULT "
_PROBE_TIMEOUT_SECONDS = 180

# The deny arms this gate declares. Named here, and cross-checked against what
# actually ran, so "the gate executed zero deny arms" cannot be a green.
_DENY_ARMS = ("write", "spawn")

_ARM_DESCRIPTION = {
    "write": "a write outside write_paths is REFUSED (Landlock's filesystem boundary)",
    "spawn": "a fork under allow_subprocess=False is REFUSED (the seccomp filter loaded)",
}


@dataclass
class Check:
    label: str
    ok: bool
    detail: str
    is_deny_arm: bool


CHECKS: list[Check] = []


def _record(label: str, ok: bool, detail: str, *, is_deny_arm: bool = False) -> None:
    CHECKS.append(Check(label, ok, detail, is_deny_arm))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def _run_probe(probe: str, backend: str) -> str | None:
    """Run *probe* against *backend* in a fresh interpreter.

    Returns ``None`` when the deny fired, else an operator-readable reason. A
    probe subprocess that crashes, hangs or prints no result returns a reason
    too — "we could not observe it" is not "it enforced", and collapsing those
    two into one green is the exact failure this gate exists to prevent.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _CHILD_SOURCE, probe, backend],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"the probe subprocess did not finish within {_PROBE_TIMEOUT_SECONDS}s"

    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            return json.loads(line[len(_SENTINEL):])["reason"]

    return (
        f"the probe subprocess produced no result (rc={proc.returncode}, "
        f"stderr={proc.stderr[-600:]!r})"
    )


def _preflight() -> int:
    """Establish — loudly, or not at all — that this is the host the gate claims,
    and return the Landlock ABI it is about to witness.

    Everything here is FATAL rather than a skip. Each condition below is one
    under which the deny arms would report nothing while the job stayed green,
    which is indistinguishable from enforcement working.
    """
    print("=== Host ===")
    print(f"platform.system()  = {platform.system()}")
    print(f"platform.machine() = {platform.machine()}")
    print(f"platform.release() = {platform.release()}")

    if platform.system() != "Linux":
        print(
            "FATAL: Landlock is Linux-only. This gate has no meaning on "
            f"{platform.system()} and must not report one."
        )
        sys.exit(2)

    from reyn.security.sandbox.backends.landlock import LandlockBackend

    backend = LandlockBackend()
    if not backend.available():
        print(
            "FATAL: LandlockBackend().available() is False — the Landlock "
            "mechanism is not even PRESENT here "
            f"(import_error={backend.import_error!r}). This job installs the "
            "sandbox-linux extra precisely so it is; if this fires, the install "
            "step or the runner kernel is broken. Reporting it as a failure, "
            "because the alternative is skipping the only deny arms in CI and "
            "calling the result green."
        )
        sys.exit(2)

    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    if not seccomp_mod.is_available():
        print(
            "FATAL: seccomp is not available (pyseccomp absent?). The spawn "
            "deny arm below is the ONLY thing in CI that witnesses the filter "
            "loading — the axis #2962 and #3020 each broke silently — so it "
            "must not be skipped."
        )
        sys.exit(2)

    print("\n=== Landlock ABI on this runner ===")
    print(f"Landlock ABI = {backend.abi_version}")
    print(
        "★ SCOPE OF A GREEN RUN: this gate witnesses THIS ABI ONLY.\n"
        "  A runner is one kernel and a kernel is one Landlock ABI; an older ABI\n"
        "  CANNOT be faked in a container (the ABI is the host kernel's, not the\n"
        "  image's). ABI 1-2 — Ubuntu 22.04 / RHEL 9 / Debian 12, most of the\n"
        "  installed base, and where #2975's FS.TRUNCATE gap lives — is NOT\n"
        "  covered by this job at all. There it stays covered only by the runtime\n"
        "  self_test() failing closed on the user's own machine. A green here is\n"
        f"  'both denies fired on ABI {backend.abi_version} today', NOT 'Landlock\n"
        "  is witnessed on Linux'."
    )

    # available() populates it and is guaranteed True above; `or 0` only keeps
    # the return type honest for a type checker.
    return backend.abi_version or 0


def main() -> int:
    abi = _preflight()

    # ── The gate's own liveness check, before anything it proves ──────────────
    #
    # The deny arms mean something only if the probes behind them CAN say no. So
    # point them at a backend that genuinely does not enforce and require a
    # failure. If a probe passes NoopBackend, every green below is decoration.
    print("\n=== Vacuity guards: the probes must be able to FAIL ===")
    for probe in _DENY_ARMS:
        reason = _run_probe(probe, "noop")
        _record(
            f"vacuity guard: the {probe}-deny probe REPORTS FAILURE against "
            f"NoopBackend (a real backend that enforces nothing)",
            reason is not None,
            reason
            if reason is not None
            else (
                "★ the probe reported NoopBackend as enforcing. NoopBackend "
                "enforces nothing by contract, so this probe cannot distinguish "
                "a sandbox from a passthrough — the deny arm using it proves "
                "nothing and this gate is decoration"
            ),
        )

    # ── The deny arms — the actual claim ─────────────────────────────────────
    print("\n=== Deny arms: a real deny must FIRE through the real Landlock wrap ===")
    for probe in _DENY_ARMS:
        reason = _run_probe(probe, "landlock")
        _record(
            f"deny arm [{probe}]: {_ARM_DESCRIPTION[probe]}",
            reason is None,
            reason if reason is not None else "the deny fired",
            is_deny_arm=True,
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    arms_run = [c for c in CHECKS if c.is_deny_arm]
    print(f"deny arms declared: {len(_DENY_ARMS)} ({', '.join(_DENY_ARMS)})")
    print(f"deny arms run:      {len(arms_run)}")

    # A gate that ran zero deny arms is green for exactly the reason a skipped
    # test is green: nothing was observed and nothing objected. Cross-check the
    # count against the declared list so a future edit that drops an arm — or
    # short-circuits before them — fails here instead of passing quietly.
    if len(arms_run) != len(_DENY_ARMS):
        print(
            f"\nFATAL: this gate declares {len(_DENY_ARMS)} deny arms but ran "
            f"{len(arms_run)}. It cannot report a witness for an arm it never "
            f"executed."
        )
        return 2

    failed = [c for c in CHECKS if not c.ok]
    for c in failed:
        print(f"  FAIL: {c.label} — {c.detail}")

    if failed:
        print(
            "\nA deny did not fire (or the probe that would have seen it is "
            "broken). That is this gate's whole subject: the sandbox is not "
            "enforcing what the docs say it enforces, on a host where it can. "
            "Do not route around this by relaxing the gate — #2962, #2980 and "
            "#3020 were each a live version of this failure that a green suite "
            "did not report for weeks. Fix the enforcement."
        )
        return 1

    print(
        f"\nBoth denies FIRED on this runner (Landlock ABI {abi}). Scope: one "
        "ABI, one kernel, the write + spawn axes through wrap_command. NOT "
        "witnessed here: ABI 1-2, the network gate, read_deny_paths "
        "(inexpressible on Landlock), or the run() preexec path."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
