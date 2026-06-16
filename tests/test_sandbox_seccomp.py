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


def test_syscall_allowlist_no_subprocess_by_default() -> None:
    """Tier 2: subprocess syscalls absent when policy.allow_subprocess is False (default)."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    assert "execve" not in result
    assert "fork" not in result


def test_syscall_allowlist_subprocess_when_enabled() -> None:
    """Tier 2: subprocess syscalls present when policy.allow_subprocess is True."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy(allow_subprocess=True))
    assert "execve" in result
    assert "fork" in result
    assert "clone" in result


def test_syscall_allowlist_excludes_destructive_syscalls() -> None:
    """Tier 2: destructive filesystem syscalls never in the allowlist (Landlock's job)."""
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    for name in ("unlink", "unlinkat", "rmdir", "rename", "mkdir"):
        assert name not in result, (
            f"Destructive syscall {name!r} must not be in seccomp allowlist"
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
# install_seccomp_filter() tests
# ---------------------------------------------------------------------------


def test_install_returns_callable() -> None:
    """Tier 2: install_seccomp_filter() returns a callable."""
    from reyn.security.sandbox.backends.seccomp import install_seccomp_filter

    result = install_seccomp_filter(SandboxPolicy())
    assert callable(result)


def test_install_callable_noops_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: calling the filter installer is safe and warns when seccomp is unavailable."""
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    # Force unavailability (macOS or pyseccomp absent — both represented by
    # monkeypatching platform.system to non-Linux, which is the macOS reality).
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    fn = seccomp_mod.install_seccomp_filter(SandboxPolicy())

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        fn()  # Must not raise.

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "Expected a WARNING log mentioning seccomp when filter installation is skipped"
    )
