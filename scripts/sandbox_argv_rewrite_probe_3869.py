"""One-shot Linux measurement for #3869: can a process under reyn's real
Landlock+seccomp restrictions (`_child_preexec`, the SAME production
callsite `sandbox_seccomp_x86_64_live_smoke.py` drives) rewrite its own
argv region (what `ps -o args=` shows) and/or its short kernel name (what
`prctl(PR_SET_NAME)` sets — the 16-byte name `/proc/pid/comm`, `top`'s
COMMAND column on Linux, reads)?

Mirrors macOS's own measurement (issue #3869: Seatbelt does not deny
`setproctitle`'s argv rewrite, confirmed against a positive control on the
same policy shape) — this closes the Linux half that measurement
explicitly left open, since Landlock/seccomp enforcement cannot be
exercised from a non-Linux host. `prctl` is already in the seccomp
allowlist (`_BASELINE`, `backends/seccomp.py`) — so the interesting
question isn't "is prctl callable" (it is), it's "does the *specific*
argv-rewrite / PR_SET_NAME mechanism setproctitle uses actually take
effect", measured end to end rather than inferred from the allowlist alone.

NOT a pytest file, matching the sibling live-smoke script's own reasoning:
loading a real default-deny seccomp filter is irrevocable for the rest of
the process, so the probe forks a fresh subprocess. This script is a
ONE-SHOT measurement (#3869 asked to measure before deciding scope, not to
ship a new permanent gate) — it ran once as a temporary extra step in
`sandbox-linux-live-x86_64.yml` (PR #3980), the result (3/3 passed: a
Landlock+seccomp-restricted child CAN rewrite both its argv and its
PR_SET_NAME) was recorded on #3869, and the CI step was then removed —
this file is kept as a standalone script for a future rerun (e.g. after a
sandbox-layer change) rather than deleted, but is not itself a gate.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass
class ProbeResult:
    label: str
    ok: bool
    detail: str


RESULTS: list[ProbeResult] = []


def _record(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ProbeResult(label, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


_CHILD_SCRIPT = """
import ctypes
import os
import sys

marker = sys.argv[1]
outcomes = {}

# Positive control (same policy shape): a write OUTSIDE any granted path
# must still be denied by Landlock's fs rules under this same run, so a
# later "argv rewrite succeeded" result cannot be a false positive from
# an inactive sandbox.
try:
    with open("/etc/argv_probe_should_be_denied_3869", "w") as f:
        f.write("should not get here")
    outcomes["positive_control_write_outside_grant"] = "UNEXPECTED_SUCCESS"
except Exception as exc:
    outcomes["positive_control_write_outside_grant"] = f"denied:{type(exc).__name__}"

try:
    import setproctitle
    setproctitle.setproctitle("reyn:probe-child")
    outcomes["setproctitle_argv_rewrite"] = "ok"
except Exception as exc:
    outcomes["setproctitle_argv_rewrite"] = f"failed:{type(exc).__name__}:{exc}"

try:
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_NAME = 15
    name = b"reyn-probe\\0"
    rc = libc.prctl(PR_SET_NAME, name, 0, 0, 0)
    if rc != 0:
        outcomes["prctl_pr_set_name"] = f"failed:errno={ctypes.get_errno()}"
    else:
        outcomes["prctl_pr_set_name"] = "ok"
except Exception as exc:
    outcomes["prctl_pr_set_name"] = f"exception:{type(exc).__name__}:{exc}"

with open(marker, "w") as f:
    for k, v in outcomes.items():
        f.write(f"{k}={v}\\n")
    f.write(f"pid={os.getpid()}\\n")
"""


def _build_ruleset_and_policy(workdir: str):
    """Same shape `sandbox_seccomp_x86_64_live_smoke.py::_validate_callsite1`
    builds — the real Landlock ruleset a production `LandlockBackend.run()`
    would construct, granting broad read + write scoped to ``workdir``.
    Falls back to ``ruleset=None`` (seccomp-only) on a host without
    Landlock, matching #2975's own aarch64 validation shape."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    policy = SandboxPolicy(write_paths=[workdir])
    backend = LandlockBackend()
    if not backend.available():
        print(
            f"Landlock NOT available (import_error={backend.import_error!r}); "
            "exercising the seccomp-only shape (ruleset=None)."
        )
        return None, policy

    import landlock  # noqa: PLC0415

    FS = landlock.FSAccess  # type: ignore[attr-defined]
    read_rules = FS.READ_FILE | FS.READ_DIR | FS.EXECUTE
    write_rules = (
        read_rules
        | FS.WRITE_FILE
        | FS.MAKE_REG | FS.MAKE_DIR | FS.MAKE_SYM
        | FS.MAKE_CHAR | FS.MAKE_BLOCK | FS.MAKE_FIFO | FS.MAKE_SOCK
        | FS.REMOVE_FILE | FS.REMOVE_DIR
    )
    ruleset = landlock.Ruleset(  # type: ignore[attr-defined]
        restrict_rules=read_rules | write_rules,
    )
    ruleset.allow("/", rules=read_rules)
    ruleset.allow(workdir, rules=write_rules)
    print(f"Landlock available (ABI {backend._abi_version}); stacking Landlock+seccomp.")
    return ruleset, policy


def _run_probe(workdir: str) -> None:
    from reyn.security.sandbox.backends.landlock import _child_preexec

    ruleset, policy = _build_ruleset_and_policy(workdir)

    marker = os.path.join(workdir, "marker.txt")
    child_path = os.path.join(workdir, "child.py")
    with open(child_path, "w") as f:
        f.write(_CHILD_SCRIPT)

    proc = subprocess.run(
        [sys.executable, child_path, marker],
        preexec_fn=lambda: _child_preexec(ruleset, policy),
        capture_output=True,
        timeout=15,
    )
    print("child stdout:", proc.stdout.decode(errors="replace"))
    print("child stderr:", proc.stderr.decode(errors="replace"))
    print("child returncode:", proc.returncode)

    if not os.path.exists(marker):
        _record(
            "child completed and wrote its marker",
            False,
            f"marker never written, returncode={proc.returncode}",
        )
        return

    outcomes: dict[str, str] = {}
    with open(marker) as f:
        for line in f:
            if "=" in line:
                k, _, v = line.strip().partition("=")
                outcomes[k] = v

    _record(
        "positive control (write outside grant must be denied)",
        outcomes.get("positive_control_write_outside_grant", "").startswith("denied"),
        outcomes.get("positive_control_write_outside_grant", "<missing>"),
    )
    _record(
        "setproctitle argv rewrite (ps -o args= visibility)",
        outcomes.get("setproctitle_argv_rewrite") == "ok",
        outcomes.get("setproctitle_argv_rewrite", "<missing>"),
    )
    _record(
        "prctl(PR_SET_NAME) (top COMMAND / /proc/pid/comm visibility)",
        outcomes.get("prctl_pr_set_name") == "ok",
        outcomes.get("prctl_pr_set_name", "<missing>"),
    )
    if "pid" in outcomes:
        print(f"child pid was {outcomes['pid']}")


def main() -> int:
    print(f"platform.system()  = {platform.system()}")
    print(f"platform.machine() = {platform.machine()}")

    if platform.system() != "Linux":
        print(
            "FATAL: this script measures Linux Landlock+seccomp enforcement "
            "specifically — the macOS half of #3869 was already measured "
            "separately (Seatbelt, see issue #3869)."
        )
        return 2

    import reyn.security.sandbox.backends.seccomp as seccomp_mod
    if not seccomp_mod.is_available():
        print(
            "FATAL: seccomp unavailable (pyseccomp missing) — install the "
            "sandbox-linux extra before running this script."
        )
        return 2

    with tempfile.TemporaryDirectory() as workdir:
        _run_probe(workdir)

    failed = [r for r in RESULTS if not r.ok]
    print()
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} probes passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
