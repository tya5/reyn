"""Tier 2: seccomp-BPF filter invariants + production wiring (FP-0017 Component B)."""
from __future__ import annotations

import logging
import os
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
    execve, so refusing execve would stop the sandboxed target before it starts —
    the sandbox would deny itself its own reason to exist (#2962). execve replaces
    the calling process and spawns nothing, so allowing it grants no subprocess
    capability; process creation is gated separately (see the tests above).
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    restrictive = _build_syscall_allowlist(SandboxPolicy(allow_subprocess=False))
    for name in ("execve", "execveat"):
        assert name in restrictive, (
            f"{name!r} must be allowed even under the most restrictive policy, "
            "or the sandboxed target is stopped before it can start"
        )


def test_syscall_allowlist_delegates_landlock_governed_writes() -> None:
    """Tier 2: filesystem syscalls Landlock GOVERNS reach it instead of being refused.

    Inverts the pre-#2962 expectation, which asserted these were absent "because
    Landlock governs writes". That reasoning does not hold under a default-deny
    filter: a syscall absent from the allowlist is not delegated to Landlock, it
    is refused before Landlock can adjudicate. Measured under the live filter,
    os.mkdir / os.remove / os.rename / shutil.rmtree were all killed.

    Scoped deliberately to rights in LandlockBackend's HANDLED set (MAKE_DIR /
    REMOVE_* / REFER / WRITE_FILE …). Only for those does "allowing it grants no
    path access" hold, because only those does Landlock actually adjudicate; see
    the companion exclusion test below.
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    result = _build_syscall_allowlist(SandboxPolicy())
    for name in ("unlinkat", "mkdirat", "renameat", "symlinkat", "getcwd"):
        assert name in result, (
            f"{name!r} is governed by Landlock's handled set, so it must reach "
            "Landlock for adjudication; absent from a default-deny allowlist it "
            "is refused here instead"
        )


def test_syscall_allowlist_excludes_syscalls_landlock_cannot_govern() -> None:
    """Tier 2: syscalls no layer would stop are never allowed on a delegation rationale.

    The counterweight to the test above, and the reason it is scoped rather than
    a blanket "filesystem syscalls are allowed". "Landlock will adjudicate it"
    justifies allowing a syscall ONLY if the right is in LandlockBackend's
    handled set:

    - chmod/chown: Landlock has no such right at all (the kernel documents
      chmod(2)/chown(2) under its current limitations), so nothing downstream
      would stop them.
    - path-based truncate: FSAccess.TRUNCATE exists (ABI 3+) but is not in the
      handled set, and granting it there would be ABI-gated — leaving the hole
      open on ABI 1-2 kernels. Excluding it is uniformly correct.

    Concretely, allowing these would let a policy with write_paths=[workspace]
    run os.chmod("~/.ssh", 0o777) or os.truncate("~/.ssh/id_ed25519", 0): passed
    by seccomp, invisible to Landlock, permitted by DAC. ftruncate is fine and
    stays — it needs an already-open fd, which Landlock adjudicates via
    WRITE_FILE.
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    permissive = _build_syscall_allowlist(
        SandboxPolicy(network=True, allow_subprocess=True)
    )
    for name in ("chmod", "fchmod", "fchmodat", "chown", "fchown", "truncate"):
        assert name not in permissive, (
            f"{name!r} must not be allowed: Landlock cannot adjudicate it, so "
            "no layer would deny it outside write_paths"
        )
    assert "ftruncate" in permissive, (
        "ftruncate acts on an open fd that Landlock already adjudicated"
    )


def test_syscall_allowlist_includes_async_runtime_and_durability_primitives() -> None:
    """Tier 2: the async-runtime startup + durability primitives are baseline (#3059).

    When #3030 made the seccomp filter load unconditionally, every stdio MCP
    server (all built on an async runtime) came under this default-deny allowlist
    for the first time. Two x86_64-CI rounds surfaced the gaps the aarch64 witness
    missed: round 1 `socketpair` (CPython asyncio self-pipe) / `eventfd` (Tokio
    mio waker) — the runtime could not start; round 2 `fsync`/`fdatasync` (SQLite
    "disk I/O error" in the vector-store) / `flock` (uv cache lock in markitdown)
    — the started server could not do local I/O. All are local-fd/IPC primitives
    (no network reach), so they belong in the baseline, not behind the network or
    subprocess gate.

    Pinned as a platform-independent builder assertion so their removal is caught
    even where Landlock is absent (macOS, `test.yml`) — the enforce-arch server
    probes in `test_sandbox_seccomp_network_3030.py` witness they are SUFFICIENT;
    this witnesses they are PRESENT.
    """
    from reyn.security.sandbox.backends.seccomp import _build_syscall_allowlist

    # Present regardless of policy — an async runtime needs these to start under
    # the most restrictive policy (network off, subprocess off).
    result = _build_syscall_allowlist(SandboxPolicy())
    for name in (
        # event-loop startup (round 1)
        "socketpair",
        "eventfd", "eventfd2",
        "timerfd_create", "timerfd_settime", "timerfd_gettime",
        "signalfd4",
        "epoll_create",
        # durability + advisory locking on already-open fds (round 2): SQLite
        # fsync (vector-store "disk I/O error") + uv cache flock (markitdown).
        "fsync", "fdatasync", "sync_file_range",
        "flock",
    ):
        assert name in result, (
            f"{name!r} must be baseline: an async runtime (asyncio/Tokio/libuv) "
            "needs it to build its event loop or persist/lock its files, and "
            "#3030 now subjects every stdio MCP server to this filter. Absent, "
            "the server cannot start or do local I/O (#3059)"
        )

    # …but they must NOT smuggle in network reach — the network syscalls stay
    # gated on policy.network (socketpair is AF_UNIX-only at the kernel, so it is
    # not one of them).
    assert "socket" not in result, (
        "socket must stay network-gated — adding the async-runtime local-fd "
        "primitives must not reopen #3030"
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
# load_seccomp_filter() tests — the MECHANISM (see the wiring section below for
# the tests that prove production actually calls it).
# ---------------------------------------------------------------------------


def test_load_has_no_deferred_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: load_seccomp_filter() loads on call and hands back nothing to invoke.

    The API contract that makes #2962 unconstructable. The old entry point
    returned an installer, so "call it and discard the result" was a silent
    no-op that read like working code at the callsite — which is exactly what
    both production callsites did for the layer's entire existence. A caller
    cannot drop a load that has no return value; the only remaining misuse is
    not calling it at all, which is visible to a reader and is what the wiring
    tests below pin.

    The Darwin guard is load-bearing, not incidental: this asserts an API shape
    and must never load a real filter. On Linux with pyseccomp installed —
    exactly the `pip install reyn[sandbox-linux]` setup the module docstring
    invites x86_64 contributors to run — an unguarded call here would install an
    irrevocable default-deny filter into the pytest process itself, and every
    later test in the session would see EPERM from clone/socket/chown. macOS CI
    cannot surface that (no pyseccomp), which is the same structural blindness
    #2962 is about.
    """
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert seccomp_mod.load_seccomp_filter(SandboxPolicy()) is None
    assert not hasattr(seccomp_mod, "build_seccomp_installer"), (
        "The builder must not come back: it is the shape that made the discard "
        "bug expressible"
    )
    assert not hasattr(seccomp_mod, "install_seccomp_filter")


def test_refused_fork_is_classifiable_as_a_sandbox_denial() -> None:
    """Tier 2: the filter's refusal surfaces as an errno denial.py (#2820) can classify.

    The filter refuses with EPERM rather than killing, which is what keeps the
    #2820 launcher-fork mitigation alive on Linux. denial.py detects that class
    by matching "fork: operation not permitted" on stderr, and its docstring
    explicitly claims to cover "Linux seccomp". A killing filter emits no errno
    and no output, so the launcher prints nothing, classify_denial returns None,
    and the mitigation silently stops working the moment the filter goes live —
    the exact shape of #2962 (a layer that is present but does nothing).

    Pins the two ends together: the errno the filter refuses with is the errno
    the launcher reports, and that report is classifiable.
    """
    import errno

    from reyn.security.sandbox.denial import DENIAL_FORK, classify_denial

    # What a PATH shim prints when its fork() is refused with the filter's errno.
    launcher_stderr = f"pyenv: fork: {os.strerror(errno.EPERM)}".encode()

    assert classify_denial(1, launcher_stderr) == DENIAL_FORK, (
        "A fork refused with EPERM must classify as the #2820 denial class; if "
        "the filter kills instead, stderr is empty and this mitigation dies"
    )
    # The killing-filter counterfactual: no errno, no output, nothing to classify.
    assert classify_denial(-31, b"") is None


def test_load_noops_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: loading the filter is safe and warns when seccomp is unavailable.

    Also establishes the observation channel the wiring tests rely on: this WARN
    is emitted if and only if load_seccomp_filter() is actually called.
    """
    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    _reset_cache()
    # Force unavailability (macOS or pyseccomp absent — both represented by
    # monkeypatching platform.system to non-Linux, which is the macOS reality).
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        seccomp_mod.load_seccomp_filter(SandboxPolicy())  # Must not raise.

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "Expected a WARNING log mentioning seccomp when filter installation is skipped"
    )


# ---------------------------------------------------------------------------
# WIRING tests (#2962) — do the production callsites INVOKE the installer?
#
# The pre-#2962 mechanism tests called the returned callable themselves, which is
# precisely why they stayed green while the layer was dead in production. These
# tests instead drive the real production child-side entry points and observe an
# effect that only occurs when the filter is actually loaded.
#
# Observation channel: on this (non-Linux) host load_seccomp_filter() logs the
# "seccomp-BPF unavailable … skipping syscall filter" WARN when called (pinned by
# test_load_noops_when_unavailable). So WARN present ⇔ the callsite called it.
# Deleting the load_seccomp_filter(policy) line from either callsite turns the
# corresponding test RED. Verified by strip: each fails with its call removed and
# passes with it restored.
# ---------------------------------------------------------------------------


def test_landlock_child_preexec_invokes_the_seccomp_installer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: LandlockBackend's preexec_fn actually loads the seccomp filter.

    Drives the real ``_child_preexec`` — the function Popen's ``preexec_fn``
    calls — with no Landlock ruleset (None) so the seccomp step is reachable off
    Linux. Guards the #2962 regression at the landlock.py:196 callsite. This
    callsite has its own syscall requirements (CPython's post-preexec_fn
    close_range), so it is verified separately from the shim below.
    """
    import reyn.security.sandbox.backends.landlock as landlock_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        landlock_mod._child_preexec(None, SandboxPolicy(allow_subprocess=False))

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "LandlockBackend's preexec_fn never loaded the seccomp filter — the "
        "layer is dead in production (#2962)"
    )


def test_landlock_child_preexec_loads_seccomp_even_when_subprocess_allowed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: LandlockBackend's preexec_fn loads the seccomp filter even when
    allow_subprocess=True (#3030 fix).

    This USED TO be the negative half of the wiring pin — allow_subprocess=True
    removed the seccomp layer entirely, silently dropping the NETWORK gate along
    with the syscall-reduction one (#3030: a real outbound connect+send SUCCEEDED
    under `network=False, allow_subprocess=True`, the stdio-MCP default). The
    filter now always loads; `_build_syscall_allowlist` is what actually widens
    the allowlist for allow_subprocess=True (adding `_SUBPROCESS_SYSCALLS`), not
    a callsite-level skip of the filter itself.
    """
    import reyn.security.sandbox.backends.landlock as landlock_mod

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        landlock_mod._child_preexec(None, SandboxPolicy(allow_subprocess=True))

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "LandlockBackend's preexec_fn skipped the seccomp filter under "
        "allow_subprocess=True — the #3030 network-gate regression"
    )


def test_landlock_exec_shim_invokes_the_seccomp_installer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the landlock_exec shim's ``_apply_seccomp`` loads the filter.

    Drives the real ``_apply_seccomp`` that ``_apply_landlock`` calls before
    ``os.execvp`` — a plain-execvp shape whose syscall needs differ from the
    LandlockBackend preexec_fn above, hence the separate verification.

    ⚠ Scope, stated because overreading it cost 41 days: this pins that
    ``_apply_seccomp`` LOADS when called, and nothing about whether anything
    calls it. It is one step below the production entry point, and #2980 lived in
    the step above — ``_apply_landlock`` raised ``AttributeError`` before
    reaching this function, so the shim applied nothing while this test stayed
    green (#2980's title: "its test bypasses the production entry point").
    What covers the entry point is
    ``test_landlock_exec_shim_1344e.py``'s enforcement group, which launches
    through ``wrap_command`` — and only where Landlock is present.
    """
    import reyn.security.sandbox.landlock_exec as shim

    _reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox.backends.seccomp"):
        shim._apply_seccomp(SandboxPolicy(allow_subprocess=False))

    assert any("seccomp" in record.message.lower() for record in caplog.records), (
        "The landlock_exec shim never loaded the seccomp filter — the layer is "
        "dead in production (#2962)"
    )
