"""Tier 2: seccomp-BPF filter builder invariants (FP-0017 Component B)."""
from __future__ import annotations

import logging
import sys

import pytest

from reyn.security.sandbox.policy import SandboxPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_cache() -> None:
    """Clear the module-level availability cache before each relevant test."""
    import reyn.security.sandbox.backends.seccomp as _seccomp_mod

    _seccomp_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# is_available() tests
# ---------------------------------------------------------------------------


def test_is_available_returns_false_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: is_available() returns False when platform is not Linux."""
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert seccomp_mod.is_available() is False


def test_is_available_returns_false_when_pyseccomp_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: is_available() returns False when pyseccomp cannot be imported."""
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Linux")
    # Remove pyseccomp from sys.modules so the import attempt inside is_available()
    # sees it as missing.
    monkeypatch.setitem(sys.modules, "pyseccomp", None)  # type: ignore[arg-type]
    assert seccomp_mod.is_available() is False


# ---------------------------------------------------------------------------
# _build_syscall_allowlist() tests — platform-independent (tests the builder)
# ---------------------------------------------------------------------------


def test_syscall_allowlist_includes_baseline() -> None:
    """Tier 2: baseline allowlist includes fundamental process syscalls."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    for name in ("read", "write", "exit_group", "brk", "mmap", "openat"):
        assert name in result, f"Expected baseline syscall {name!r} in allowlist"


def test_syscall_allowlist_no_network_by_default() -> None:
    """Tier 2: network syscalls absent when policy.network is False (default)."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    assert "socket" not in result
    assert "connect" not in result


def test_syscall_allowlist_network_when_enabled() -> None:
    """Tier 2: network syscalls present when policy.network is True."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy(network=True))
    assert "socket" in result
    assert "connect" in result
    assert "accept" in result


def test_syscall_allowlist_no_process_creation_by_default() -> None:
    """Tier 2: process-CREATION syscalls absent when allow_subprocess is False (default)."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    for name in ("fork", "vfork", "clone", "clone3"):
        assert name not in result, (
            f"Process-creation syscall {name!r} must be denied when "
            "allow_subprocess is False"
        )


def test_syscall_allowlist_process_creation_when_enabled() -> None:
    """Tier 2: process-creation syscalls present when policy.allow_subprocess is True."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy(allow_subprocess=True))
    assert "fork" in result
    assert "clone" in result


def test_syscall_allowlist_always_permits_exec_of_the_sandboxed_target() -> None:
    """Tier 2: execve/execveat are allowed even when allow_subprocess is False.

    Both callsites load the filter in a pre-exec position and the filter survives
    execve, so denying execve would KILL the sandboxed target before it starts —
    the sandbox would deny itself its own reason to exist (#2962). execve replaces
    the calling process and spawns nothing, so allowing it grants no subprocess
    capability; process creation is gated separately (see the tests above).
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    restrictive = _build_syscall_allowlist(SandboxPolicy(allow_subprocess=False))
    for name in ("execve", "execveat"):
        assert name in restrictive, (
            f"{name!r} must be allowed even under the most restrictive policy, "
            "or the sandboxed target is killed before it can start"
        )


def test_syscall_allowlist_delegates_filesystem_writes_to_landlock() -> None:
    """Tier 2: filesystem-mutating syscalls reach Landlock rather than being KILLed.

    Inverts the pre-#2962 expectation, which asserted these were absent "because
    Landlock governs writes". That reasoning does not hold under defaction=KILL:
    a syscall absent from a default-deny allowlist is not delegated to Landlock,
    it kills the process with SIGSYS before Landlock can adjudicate. Measured
    under the live filter, os.mkdir / os.remove / os.rename / shutil.rmtree were
    all killed. Allowing them here grants no path access — Landlock still denies
    writes outside policy.write_paths — it is what makes Landlock the deciding
    layer, as the module always claimed.
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    for name in ("unlinkat", "mkdirat", "renameat", "getcwd"):
        assert name in result, (
            f"{name!r} must reach Landlock for adjudication; absent from a "
            "default-deny KILL allowlist it terminates the process instead"
        )


def test_syscall_allowlist_excludes_escape_hatches() -> None:
    """Tier 2: escape-hatch syscalls never allowed regardless of policy."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    # Test with the most permissive policy to confirm exclusion is unconditional.
    full_policy = SandboxPolicy(network=True, allow_subprocess=True)
    result = _build_syscall_allowlist(full_policy)
    for name in ("ptrace", "process_vm_readv", "keyctl", "modify_ldt", "request_key"):
        assert name not in result, (
            f"Escape-hatch syscall {name!r} must never appear in seccomp allowlist"
        )


# ---------------------------------------------------------------------------
# build_seccomp_installer() tests — the MECHANISM (see the wiring section below
# for the tests that prove production actually invokes it).
# ---------------------------------------------------------------------------


def test_build_returns_callable() -> None:
    """Tier 2: build_seccomp_installer() returns a callable."""
    from reyn.security.sandbox.backends.seccomp import build_seccomp_installer

    result = build_seccomp_installer(SandboxPolicy())
    assert callable(result)


def test_build_alone_has_no_side_effect(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 2: build_seccomp_installer() does nothing until the installer is invoked.

    This is the property that made #2962 invisible: a callsite that builds and
    discards is indistinguishable from one that never called at all. It is also
    what gives the wiring tests below their teeth — the "filter skipped" WARN is
    emitted ONLY on invocation, so its presence is evidence of invocation.
    """
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        seccomp_mod.build_seccomp_installer(SandboxPolicy())  # built, never invoked

    assert not caplog.records, (
        "build_seccomp_installer() must be side-effect-free until invoked; "
        f"got log records: {[r.message for r in caplog.records]}"
    )


def test_installer_noops_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: invoking the installer is safe and warns when seccomp is unavailable."""
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    # Force unavailability (macOS or pyseccomp absent — both represented by
    # monkeypatching platform.system to non-Linux, which is the macOS reality).
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    fn = seccomp_mod.build_seccomp_installer(SandboxPolicy())

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        fn()  # Must not raise.

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "Expected a WARNING log mentioning seccomp when filter installation is skipped"
    )


# ---------------------------------------------------------------------------
# WIRING tests (#2962) — do the production callsites INVOKE the installer?
#
# The mechanism tests above all call the returned callable themselves, which is
# precisely why they stayed green while the layer was dead in production. These
# tests instead drive the real production child-side entry points and observe an
# effect that only occurs on invocation.
#
# Observation channel: on this (non-Linux) host the installer logs the
# "seccomp-BPF unavailable … skipping syscall filter" WARN when invoked, and
# logs NOTHING when merely built (pinned by test_build_alone_has_no_side_effect).
# So WARN present ⇔ the callsite invoked the installer. Restoring the #2962 bug
# (dropping the `installer()` line) turns each of these RED. Verified by strip:
# both fail with the invoke removed, both pass with it restored.
# ---------------------------------------------------------------------------


def test_landlock_child_preexec_invokes_the_seccomp_installer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: LandlockBackend's preexec_fn loads the seccomp filter, not just builds it.

    Drives the real ``_child_preexec`` — the function Popen's ``preexec_fn``
    calls — with no Landlock ruleset (None) so the seccomp step is reachable off
    Linux. Guards the #2962 regression at the landlock.py:196 callsite.
    """
    import reyn.security.sandbox.backends.landlock as landlock_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        landlock_mod._child_preexec(None, SandboxPolicy(allow_subprocess=False))

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "LandlockBackend's preexec_fn built the seccomp installer but never invoked "
        "it — the filter is dead in production (#2962)"
    )


def test_landlock_child_preexec_skips_seccomp_when_subprocess_allowed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: LandlockBackend's preexec_fn installs no seccomp filter when subprocess is allowed.

    The negative half of the wiring pin: without it, a callsite that invoked the
    installer unconditionally would also pass the positive test above. Documents
    today's gate — allow_subprocess=True removes the seccomp layer entirely
    (#2962 flags this as the open design question; not changed here).
    """
    import reyn.security.sandbox.backends.landlock as landlock_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        landlock_mod._child_preexec(None, SandboxPolicy(allow_subprocess=True))

    assert not caplog.records, (
        "Expected no seccomp activity when allow_subprocess=True; "
        f"got: {[r.message for r in caplog.records]}"
    )


def test_landlock_exec_shim_invokes_the_seccomp_installer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the landlock_exec shim loads the seccomp filter, not just builds it.

    Drives the real ``_apply_seccomp`` that ``_apply_landlock`` calls before
    ``os.execvp``. Guards the #2962 regression at the landlock_exec.py:135
    callsite.
    """
    import reyn.security.sandbox.landlock_exec as shim

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        shim._apply_seccomp(SandboxPolicy(allow_subprocess=False))

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "The landlock_exec shim built the seccomp installer but never invoked it "
        "— the filter is dead in production (#2962)"
    )
