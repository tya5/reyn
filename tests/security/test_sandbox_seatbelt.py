"""Tier 2: SeatbeltBackend invariants (FP-0017 Component C).

★ CI-visibility gap (#3881, owner ruling 2026-09-04: no macOS CI runner will
be added — every job in this repo's CI runs on ubuntu-latest): 5 of this
module's 24 tests are Darwin-gated (`skipif(sys.platform != "darwin", ...)`,
sandbox-exec is macOS-only) and have therefore NEVER executed in CI. A green
CI run of this module does not mean those 5 passed — it means they never
ran. They exercise only on a real macOS machine.
"""
from __future__ import annotations

import sys

import pytest

from reyn.security.sandbox.backend import SandboxBackend
from reyn.security.sandbox.backends.seatbelt import (
    SeatbeltBackend,
    _build_sbpl_profile,
    _sbpl_quote,
)
from reyn.security.sandbox.policy import SandboxPolicy, expand_policy_path

# ─── 1. Availability ──────────────────────────────────────────────────────────


def test_seatbelt_unavailable_on_non_darwin(monkeypatch):
    """Tier 2: SeatbeltBackend.available() returns False on non-Darwin platforms."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert SeatbeltBackend().available() is False


def test_seatbelt_unavailable_when_sandbox_exec_missing(monkeypatch):
    """Tier 2: SeatbeltBackend.available() returns False when sandbox-exec is absent."""
    import platform
    import shutil

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("14.5.0", ("", "", ""), ""))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert SeatbeltBackend().available() is False


# ─── 2. SBPL profile generation ──────────────────────────────────────────────


def test_sbpl_profile_default_deny():
    """Tier 2: _build_sbpl_profile() always includes (deny default)."""
    policy = SandboxPolicy()
    profile = _build_sbpl_profile(policy)
    assert "(deny default)" in profile


def test_sbpl_profile_broad_read():
    """Tier 2: #1199 realignment — reads are broad by default; the profile emits a
    blanket (allow file-read*) rule (a standalone line, not a per-path subpath)."""
    policy = SandboxPolicy(read_deny_paths=[])
    profile = _build_sbpl_profile(policy)
    # Exact-line check: distinguishes the broad rule from per-path
    # `(allow file-read* (subpath ...))` rules that merely share the prefix.
    assert "(allow file-read*)" in profile.splitlines()


def test_sbpl_profile_read_deny_paths_after_broad_allow():
    """Tier 2: read_deny_paths emit (deny file-read* (subpath ...)) AFTER the broad
    (allow file-read*), so SBPL last-match-wins makes the deny win for those paths."""
    from pathlib import Path

    deny_raw = "/tmp/secretz"
    resolved = str(Path(deny_raw).expanduser().resolve(strict=False))
    policy = SandboxPolicy(read_deny_paths=[deny_raw])
    profile = _build_sbpl_profile(policy)
    deny_rule = f'(deny file-read* (subpath "{resolved}"))'
    assert "(allow file-read*)" in profile
    assert deny_rule in profile
    # Ordering matters under last-match-wins: the broad allow must come first.
    assert profile.index("(allow file-read*)") < profile.index(deny_rule)


def test_sbpl_profile_bare_default_carries_no_sensitive_deny():
    """Tier 2: #3901 PR-B ④ (owner ruling B, full compat) — a bare
    ``SandboxPolicy()`` no longer carries the OS-level sensitive deny-list;
    ``read_deny_paths`` defaults to empty. A caller that wants the ~/.ssh-etc
    defense-in-depth (e.g. the MCP client, which runs untrusted third-party
    code) sets ``read_deny_paths=list(DEFAULT_SENSITIVE_READ_DENY)``
    explicitly — see test_sbpl_profile_explicit_sensitive_deny_list below for
    that opt-in leg."""
    from pathlib import Path

    profile = _build_sbpl_profile(SandboxPolicy())
    ssh_resolved = str(Path("~/.ssh").expanduser().resolve(strict=False))
    assert f'(deny file-read* (subpath "{ssh_resolved}"))' not in profile


def test_sbpl_profile_explicit_sensitive_deny_list():
    """Tier 2: the opt-in leg — declaring ``read_deny_paths`` explicitly (e.g.
    with :data:`DEFAULT_SENSITIVE_READ_DENY`) still excludes ~/.ssh etc from
    the broad read surface (defense-in-depth, now opt-in rather than default)."""
    from pathlib import Path

    from reyn.security.sandbox.policy import DEFAULT_SENSITIVE_READ_DENY

    profile = _build_sbpl_profile(
        SandboxPolicy(read_deny_paths=list(DEFAULT_SENSITIVE_READ_DENY))
    )
    ssh_resolved = str(Path("~/.ssh").expanduser().resolve(strict=False))
    assert f'(deny file-read* (subpath "{ssh_resolved}"))' in profile


def test_sbpl_profile_write_paths_imply_read():
    """Tier 2: write_paths produce both file-write* and file-read* rules for each path."""
    from pathlib import Path

    raw = "/tmp/y"
    resolved = str(Path(raw).resolve(strict=False))
    policy = SandboxPolicy(write_paths=[raw])
    profile = _build_sbpl_profile(policy)
    assert f"(allow file-write* (subpath \"{resolved}\"))" in profile
    # write_paths must also emit a file-read* rule for the same path.
    assert f"(allow file-read* (subpath \"{resolved}\"))" in profile


def test_sbpl_profile_write_paths_expands_tilde():
    """Tier 2: a ``~``-relative ``write_paths`` entry expands to an absolute
    path in the emitted SBPL string.

    #3881 ① — CI-safe structural twin of
    ``test_2976_mcp_sandbox_write_paths.py::test_tilde_write_grant_actually_permits_the_write``,
    which needs a real macOS kernel and stays darwin-only.

    A ``~``-relative ``write_paths`` entry must appear in the SBPL string
    ALREADY EXPANDED to an absolute path — the #2976 bug class this pins is
    a construction bug (reyn's own ``expand_policy_path`` call being
    skipped/removed), not a kernel-enforcement question: an unexpanded ``~``
    would literally emit ``(subpath ".../~/target")`` (a path under the
    CURRENT working directory named literally ``~``, never a real
    directory), which is verifiable as a plain string fact with no
    sandbox-exec involved. Whether the kernel actually HONOURS the
    (correctly expanded) grant remains the darwin-only test's own job — this
    test cannot and does not claim to answer that."""
    import os
    from pathlib import Path

    raw = "~/reyn_2976_probe_dir"
    resolved = str(expand_policy_path(raw).resolve(strict=False))
    policy = SandboxPolicy(write_paths=[raw])
    profile = _build_sbpl_profile(policy)
    assert f'(allow file-write* (subpath "{resolved}"))' in profile
    # The un-expanded literal form (what a regressed expand_policy_path call
    # would emit) must NOT appear — this is the actual bug #2976 hit.
    literal = str(Path(os.getcwd()) / raw)
    assert f'(subpath "{literal}")' not in profile


def test_sbpl_profile_write_deny_paths_after_broad_write_allow():
    """Tier 2: a ``write_deny_paths`` entry emits its deny line AFTER the
    broader ``write_paths`` grant that engulfs it.

    #3881 ① — CI-safe structural twin of
    ``test_2978_deny_always_wins.py::test_deny_wins_over_overlapping_write_grant_read_and_write``,
    which needs a real macOS kernel and stays darwin-only.

    Mirrors ``test_sbpl_profile_read_deny_paths_after_broad_allow`` above,
    but for the WRITE axis: a ``write_deny_paths`` entry engulfed by a
    broader ``write_paths`` grant must emit its ``(deny file-write* ...)``
    line AFTER the write grant's own ``(allow file-write* ...)`` line, so
    SBPL's last-match-wins semantics let the deny win. This axis had no
    structural (order-only) coverage before — only the read axis did — even
    though #2978's own darwin-only test exercises both axes together.
    Whether last-match-wins is ACTUALLY how the macOS SBPL engine resolves
    overlapping rules is #2978's own darwin-only test's job, not this one's
    — this test only pins reyn's own emission ORDER."""
    write_raw = "/tmp/y"
    deny_raw = "/tmp/y/secret"
    write_resolved = str(expand_policy_path(write_raw).resolve(strict=False))
    deny_resolved = str(expand_policy_path(deny_raw).resolve(strict=False))
    policy = SandboxPolicy(write_paths=[write_raw], write_deny_paths=[deny_raw])
    profile = _build_sbpl_profile(policy)
    write_rule = f'(allow file-write* (subpath "{write_resolved}"))'
    deny_rule = f'(deny file-write* (subpath "{deny_resolved}"))'
    assert write_rule in profile
    assert deny_rule in profile
    assert profile.index(write_rule) < profile.index(deny_rule)


def test_sbpl_profile_network_allow():
    """Tier 2: network=True adds (allow network*); network=False omits it."""
    profile_allow = _build_sbpl_profile(SandboxPolicy(network=True))
    assert "(allow network*)" in profile_allow

    profile_deny = _build_sbpl_profile(SandboxPolicy(network=False))
    assert "(allow network*)" not in profile_deny


def test_sbpl_profile_loopback_bind_always_allowed():
    """Tier 2: a localhost-only `network-bind` is emitted regardless of
    policy.network (#3060) — the Seatbelt mirror of seccomp's `socket`/`bind`
    exception. Neither `network-outbound` nor `network-inbound` is implied by
    this rule alone; those stay carried by the policy.network-gated
    `(allow network*)` block above."""
    expected = '(allow network-bind (local ip "localhost:*"))'

    profile_off = _build_sbpl_profile(SandboxPolicy(network=False))
    assert expected in profile_off
    assert "(allow network*)" not in profile_off

    profile_on = _build_sbpl_profile(SandboxPolicy(network=True))
    assert expected in profile_on


def test_sbpl_profile_security_server_mach_lookup_always_allowed():
    """Tier 2: #4932/#4933 — the `com.apple.SecurityServer` mach-lookup grant
    is emitted regardless of policy (default-on, not gated by any
    SandboxPolicy field — owner ruling: "if this makes `gh` work under the
    default config, go ahead"). It is `global-name`-scoped, not a blanket
    `(allow mach-lookup)` — real measurement (architect, #4932, on raw
    `sandbox-exec` — a 9-candidate-SBPL-class elimination, not re-derived
    against the real backend here) found this ONE service is what both
    `security`/Keychain and `gh auth status` (which shells out to
    `security` for its stored token) need and nothing else was required."""
    expected = '(allow mach-lookup (global-name "com.apple.SecurityServer"))'

    # Present under a bare-default policy...
    assert expected in _build_sbpl_profile(SandboxPolicy())
    # ...and under a maximally-restrictive policy (deny_subprocess + no
    # network) — this grant is NOT gated by any policy field.
    assert expected in _build_sbpl_profile(
        SandboxPolicy(deny_subprocess=True, network=False)
    )
    # Never a blanket grant — the exact global-name form only.
    assert "(allow mach-lookup)" not in _build_sbpl_profile(SandboxPolicy()).splitlines()


# ─── 3. _sbpl_quote ──────────────────────────────────────────────────────────


def test_sbpl_quote_escapes_quotes_and_backslashes():
    """Tier 2: _sbpl_quote escapes backslashes and double-quotes correctly."""
    result = _sbpl_quote('/tmp/foo"bar\\baz')
    # backslash → \\, double-quote → \"
    assert result == '"/tmp/foo\\"bar\\\\baz"'


# ─── 4. Execution (darwin-only) ───────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_runs_echo_under_sandbox():
    """Tier 2: SeatbeltBackend runs /bin/echo under sandbox and captures stdout."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    # #3901 PR-B ④: read_paths was removed (dead since #1199's broad-read
    # realignment — reads are broad by default on Seatbelt too).
    policy = SandboxPolicy(timeout_seconds=10)
    result = await backend.run(["/bin/echo", "hello"], policy)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert b"hello" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_timeout_returns_minus_one():
    """Tier 2: SeatbeltBackend returns returncode=-1 when the process times out."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(timeout_seconds=1)
    result = await backend.run(["/bin/sleep", "5"], policy)
    assert result.returncode == -1


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_allows_loopback_bind_but_denies_connect_when_network_false():
    """Tier 2: #3060 — under network=False the Seatbelt sandbox still allows a
    loopback bind (the shape urllib3's import-time IPv6-support probe uses:
    `socket()` then `bind(("::1", 0))`, never a `connect()`) but continues to
    refuse an actual outbound connect() — the real egress claim."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(network=False, timeout_seconds=10)
    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n"
        "s.bind(('::1', 0))\n"
        "print('BIND_OK')\n"
        "try:\n"
        "    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    c.connect(('93.184.216.34', 80))\n"
        "    print('CONNECT_SUCCEEDED')\n"
        "except PermissionError:\n"
        "    print('CONNECT_DENIED')\n"
    )
    result = await backend.run([sys.executable, "-c", code], policy)
    assert b"BIND_OK" in result.stdout, (
        f"loopback bind must succeed under network=False (#3060); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert b"CONNECT_DENIED" in result.stdout, (
        f"outbound connect() must stay refused under network=False; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_allows_socketpair_sendto_but_denies_addressed_sendto_when_network_false():
    """Tier 2: #3060 case-(b) — the Seatbelt counterpart of the seccomp
    NULL-addr rule. Under network=False a connected AF_UNIX socketpair
    send/recv (the async event-loop self-pipe) SUCCEEDS while an ADDRESSED UDP
    ``sendto`` (real egress) stays DENIED.

    Seatbelt needs NO code change for this (unlike seccomp's explicit
    ``sendto arg4==0`` rule): SBPL's ``(allow network*)`` gate governs NETWORK
    sockets, and an AF_UNIX socketpair is not one — so the self-pipe already
    works while ``network-outbound`` on an AF_INET datagram stays refused. This
    test PINS that property so a future SBPL tightening cannot silently break
    the async runtime, and proves the egress form is still denied."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(network=False, timeout_seconds=10)
    code = (
        "import socket\n"
        "a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
        "a.send(b'ping')\n"
        "print('SOCKETPAIR_OK', b.recv(4))\n"
        "try:\n"
        "    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "    u.sendto(b'x', ('93.184.216.34', 53))\n"
        "    print('ADDRESSED_SENDTO_SUCCEEDED')\n"
        "except (PermissionError, OSError):\n"
        "    print('ADDRESSED_SENDTO_DENIED')\n"
    )
    result = await backend.run([sys.executable, "-c", code], policy)
    assert b"SOCKETPAIR_OK" in result.stdout, (
        f"connected socketpair send/recv (async self-pipe) must succeed under "
        f"network=False (#3060); stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert b"ADDRESSED_SENDTO_DENIED" in result.stdout, (
        f"addressed UDP sendto (real egress) must stay denied under network=False; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_security_command_succeeds_under_default_policy():
    """Tier 2: #4932/#4933 — `security list-keychains` (which needs the
    `com.apple.SecurityServer` mach-lookup service this profile now always
    grants) succeeds through the REAL SeatbeltBackend.run() path under a
    bare-default SandboxPolicy — not a raw `sandbox-exec` invocation
    (architect's own #4932 measurement used the latter; lead-coder's
    review explicitly asked for reproduction through the real backend +
    its full deny-list-included profile). `security` (not the personal
    `gh` CLI, whose success depends on this machine's own stored OAuth
    token) needs no machine-specific credential — just the ability to
    enumerate this machine's own keychain search list.

    ⚠️ This does NOT mean CI verifies the live capability (architect's
    post-merge correction, #4932): every workflow in `.github/workflows/`
    runs on `ubuntu-latest`, so this darwin-only test is skipped 100% of
    the time in CI, always. What CI actually protects is the sibling
    deterministic test above (the grant literal is in the generated
    profile) — this live test's own witness is a one-time measurement
    by whoever runs it locally on a real Mac, not a standing CI guard.

    Strip-falsify: this test fails (returncode != 0, "One or more
    parameters passed to a function were not valid" — the exact error
    architect measured before the fix) if the mach-lookup grant this PR
    adds is removed from ``_build_sbpl_profile``."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(timeout_seconds=10)
    result = await backend.run(["security", "list-keychains"], policy)
    assert result.returncode == 0, (
        f"security list-keychains must succeed under the default policy "
        f"(#4932/#4933); stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ─── 5. Protocol conformance ─────────────────────────────────────────────────


def test_seatbelt_conforms_to_sandbox_backend_protocol():
    """Tier 2: SeatbeltBackend satisfies the runtime-checkable SandboxBackend Protocol."""
    assert isinstance(SeatbeltBackend(), SandboxBackend)


# ─── 6. Session-scoped profile cache (#4434 stage 1) ─────────────────────────


@pytest.fixture(autouse=True)
def _reset_derivation_cache():
    """Every test in this module gets a clean process-wide derivation cache —
    without this, a policy object from an earlier test could still be alive
    (e.g. held by a still-open temp-file handle) and collide by id() with a
    freshly-constructed policy in a LATER test, since id() reuse is exactly
    the failure mode ``_derivation_cache``'s weakref eviction exists to
    close for production, not for a same-process test run reusing whatever
    ids happen to be free."""
    from reyn.security.sandbox import _derivation_cache

    _derivation_cache._reset_cache_for_tests()
    yield
    _derivation_cache._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_dead_pid_sweep_flag():
    """#5985: `_sweep_dead_pid_cache_dirs` runs at most ONCE per process (a
    module-level flag, not per-call) — without resetting it, whichever test
    in the WHOLE pytest session first triggers a Seatbelt cache-miss
    consumes that "once" for every other test that runs after it in the
    SAME process, silently no-opping the sweep in every test that actually
    means to exercise it."""
    import reyn.security.sandbox.backends.seatbelt as _seatbelt_module

    _seatbelt_module._swept_dead_pid_dirs = False
    yield
    _seatbelt_module._swept_dead_pid_dirs = False


def test_seatbelt_cache_dir_is_outside_a_realistic_write_scope():
    """Tier 2: #4434's load-bearing precondition, derived from the policy
    object (via the same expand_policy_path + resolve every emitted SBPL
    write-grant uses) — not a literal path comparison. Exercises the 3 real
    write_paths shapes issue #4434 measured (config/loader.py's empty
    default, router_op_context.py's workspace dir, and an explicit path)."""
    from pathlib import Path

    from reyn.security.sandbox.backends.seatbelt import (
        _profile_is_safe_to_cache,
        _seatbelt_cache_dir,
    )

    cache_dir = _seatbelt_cache_dir().resolve(strict=False)

    for write_paths in ([], [str(Path.cwd())], ["~/some/workspace"]):
        policy = SandboxPolicy(write_paths=write_paths)
        assert _profile_is_safe_to_cache(policy) is True
        for raw in policy.write_paths:
            write_scope = expand_policy_path(raw).resolve(strict=False)
            assert cache_dir != write_scope
            assert not cache_dir.is_relative_to(write_scope)


def test_seatbelt_cache_unsafe_when_write_scope_relocates_onto_the_cache_dir():
    """Tier 2: strip-falsify — moving a policy's write_paths to cover the
    cache directory — the exact relocation #4434's precondition exists to
    catch — flips ``_profile_is_safe_to_cache`` to False. Proves the check
    is a real, live derivation from the policy object, not a check that
    would stay green regardless of what write_paths says."""
    from reyn.security.sandbox.backends.seatbelt import (
        _profile_is_safe_to_cache,
        _seatbelt_cache_dir,
    )

    cache_dir = _seatbelt_cache_dir()

    # Exact match — the write grant covers the cache dir itself.
    assert _profile_is_safe_to_cache(SandboxPolicy(write_paths=[str(cache_dir)])) is False
    # write_scope is an ANCESTOR of the cache dir (grant on the PARENT) —
    # cache_dir is a descendant of the grant, so it's covered too: unsafe.
    assert _profile_is_safe_to_cache(
        SandboxPolicy(write_paths=[str(cache_dir.parent)]),
    ) is False
    # write_scope is a DESCENDANT of the cache dir (grant on a CHILD) — the
    # subpath grant covers only that child and below, never its own parent
    # (the cache dir itself), so this is safe.
    assert _profile_is_safe_to_cache(
        SandboxPolicy(write_paths=[str(cache_dir / "nested")]),
    ) is True


def test_seatbelt_wrap_command_reuses_the_same_profile_path_for_the_same_policy():
    """Tier 2: #4434 — two wrap_command() calls with the SAME policy object
    return the same on-disk profile path (the session-cache hit), and the
    file's content matches what _build_sbpl_profile derives for that policy
    — a real, on-disk witness that the cached path is not a stale/blank
    file, not just path-string equality."""
    from reyn.security.sandbox.backends.seatbelt import _build_sbpl_profile

    backend = SeatbeltBackend()
    policy = SandboxPolicy(write_paths=[])

    wrapped1 = backend.wrap_command(["/bin/echo", "hi"], policy)
    wrapped2 = backend.wrap_command(["/bin/echo", "hi"], policy)

    path1 = wrapped1.argv[wrapped1.argv.index("-f") + 1]
    path2 = wrapped2.argv[wrapped2.argv.index("-f") + 1]
    assert path1 == path2

    with open(path1, encoding="utf-8") as fh:
        assert fh.read() == _build_sbpl_profile(policy)

    # #5981 co-vet: cleanup() on a cached path is a REFCOUNTED release, not
    # an unconditional no-op — wrapped1 and wrapped2 are two independent
    # checkouts of the SAME cached derivation, so releasing wrapped1's alone
    # must not unlink a file wrapped2's own (still outstanding) checkout may
    # still need. Confirmed by asserting it survives past the FIRST cleanup.
    wrapped1.cleanup()
    assert __import__("os").path.exists(path1)

    # The LAST outstanding checkout's cleanup() DOES release it.
    wrapped2.cleanup()
    assert not __import__("os").path.exists(path1)


def test_seatbelt_cached_profile_is_unlinked_when_the_policy_is_collected():
    """Tier 2: #5981 — the cached `.sb` file's actual bounding subject.
    Before this, `cleanup()`'s deliberate no-op on a cached path (asserted
    just above) meant NOTHING ever unlinked it — this proves the file now
    has a real exit: the same weakref eviction that already clears the
    in-memory `_derivation_cache` entry when *policy* is collected."""
    import gc
    import os

    backend = SeatbeltBackend()

    def _wrap_and_get_path() -> str:
        # Confined to its own frame — same reasoning as
        # test_derivation_cache_5981.py's identically-shaped helper: the
        # local `policy` binding must be gone the instant this returns for
        # `gc.collect()` below to actually collect it.
        policy = SandboxPolicy(write_paths=[])
        wrapped = backend.wrap_command(["/bin/echo", "hi"], policy)
        return wrapped.argv[wrapped.argv.index("-f") + 1]

    path = _wrap_and_get_path()
    # CPython frees the confined frame's `policy` (a refcount-only, non-
    # cyclic object) the instant `_wrap_and_get_path` returns, so the file
    # may already be gone by this point — `gc.collect()` below is the
    # portable way to demand eviction, not a wait for something pending.
    gc.collect()

    assert not os.path.exists(path)


def test_seatbelt_cached_profile_survives_while_the_wrapped_command_is_held_even_with_no_separate_policy_variable():
    """Tier 2: #5981 strip-falsify caught this — an EARLIER version of the
    #5981 fix evicted the file the instant `wrap_command` returned when the
    caller passed a bare ``SandboxPolicy(...)`` inline (no local variable of
    its own, exactly what several already-existing tests in this repo do —
    e.g. ``test_seatbelt_wrap_command_prepends_sandbox_exec``), because
    NOTHING kept *policy* alive once the call returned. The fix: `_cleanup`'s
    closure captures *policy* too, so holding `wrapped` (which every caller
    must, to call `.cleanup()` eventually) keeps the file alive regardless
    of whether the caller ALSO kept its own reference to the policy."""
    import os

    backend = SeatbeltBackend()
    # Deliberately no local binding for the policy — the exact inline shape
    # that exposed the gap.
    wrapped = backend.wrap_command(["/bin/echo", "hi"], SandboxPolicy(write_paths=[]))
    path = wrapped.argv[wrapped.argv.index("-f") + 1]

    import gc

    gc.collect()  # the inline SandboxPolicy(...) has no OTHER referent now

    assert os.path.exists(path), (
        "the cached profile vanished while `wrapped` (and therefore its "
        "cleanup()) was still held — #5981's fix must keep `policy` alive "
        "via the `_cleanup` closure for exactly this reason"
    )

    # #5981 co-vet: cleanup() is now a refcounted release, not an
    # unconditional no-op — this is the ONLY checkout of this policy in this
    # test, so releasing it IS the last outstanding checkout and DOES unlink.
    wrapped.cleanup()
    assert not os.path.exists(path)


def test_seatbelt_wrap_command_does_not_cache_when_write_scope_is_unsafe():
    """Tier 2: strip-falsify — a policy whose write_paths covers the cache
    directory gets an UNCACHED, per-call profile (pre-#4434 behaviour) —
    two calls get DIFFERENT paths, and cleanup() DOES unlink. Proves the
    safety check is load-bearing on the actual wrap_command() path, not
    just on the helper function in isolation."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_dir

    backend = SeatbeltBackend()
    policy = SandboxPolicy(write_paths=[str(_seatbelt_cache_dir())])

    wrapped1 = backend.wrap_command(["/bin/echo", "hi"], policy)
    wrapped2 = backend.wrap_command(["/bin/echo", "hi"], policy)

    path1 = wrapped1.argv[wrapped1.argv.index("-f") + 1]
    path2 = wrapped2.argv[wrapped2.argv.index("-f") + 1]
    assert path1 != path2

    import os

    assert os.path.exists(path1)
    wrapped1.cleanup()
    assert not os.path.exists(path1)
    wrapped2.cleanup()


# ─── 7. Dead-pid cache-dir sweep (#5985) ────────────────────────────────────


def _dead_pid() -> int:
    """A real, guaranteed-not-alive pid — spawn a trivial subprocess and let
    it exit, then use its own pid. Cheaper and more honest than guessing a
    large integer that MIGHT collide with something real on a busy
    machine — this is an ACTUAL process that ACTUALLY exited, not a faked
    liveness answer."""
    import subprocess
    import sys as _sys

    proc = subprocess.Popen([_sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_sweep_removes_a_dead_pids_subdirectory():
    """Tier 2: a sibling pid-subdirectory whose owning process has already
    exited IS removed by the sweep — the "band question 1" answer #5985
    exists to provide (crash/SIGKILL leftovers had no bounding subject
    before this)."""

    from reyn.security.sandbox.backends.seatbelt import (
        _seatbelt_cache_root,
        _sweep_dead_pid_cache_dirs,
    )

    root = _seatbelt_cache_root()
    dead = root / str(_dead_pid())
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "leftover.sb").write_text("stale", encoding="utf-8")

    _sweep_dead_pid_cache_dirs()

    assert not dead.exists()


def test_sweep_does_not_remove_a_live_pids_subdirectory():
    """Tier 2: strip-falsify's counterpart — a sibling pid-subdirectory
    whose owning process is ALIVE (this test's own parent process, a real,
    distinct, genuinely-running pid — not the test's own pid, which the
    sweep already skips unconditionally) survives the sweep untouched.

    NON-VACUITY (strip-falsified locally, in-file Edit → run → Edit back):
    replacing the ``pid_alive(pid)`` check in ``_sweep_dead_pid_cache_dirs``
    with an unconditional False makes THIS assertion fail — the
    false-reject half of #5985's own "both directions" acceptance
    criterion (the false-accept half is
    ``test_sweep_removes_a_dead_pids_subdirectory`` above)."""
    import os

    from reyn.security.sandbox.backends.seatbelt import (
        _seatbelt_cache_root,
        _sweep_dead_pid_cache_dirs,
    )

    root = _seatbelt_cache_root()
    live_pid = os.getppid()  # the pytest runner's own parent — really alive
    live = root / str(live_pid)
    live.mkdir(parents=True, exist_ok=True)
    (live / "still-needed.sb").write_text("in use", encoding="utf-8")

    try:
        _sweep_dead_pid_cache_dirs()
        assert live.exists(), (
            "a LIVE sibling's cache dir was removed — this is exactly the "
            "failure #5981's own investigation ruled out a blanket sweep "
            "over: a concurrently-running process's sandbox-exec would now "
            "reference a deleted profile"
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(live, ignore_errors=True)  # tidy up regardless of outcome


def test_sweep_ignores_non_pid_shaped_and_non_directory_entries():
    """Tier 2: a stray file or a non-numeric-named directory under the cache
    root (never written by this module, but the population isn't
    guaranteed-pure — it's a real, shared OS temp subdirectory) is left
    alone rather than raising or being swept as if it were a pid."""
    from reyn.security.sandbox.backends.seatbelt import (
        _seatbelt_cache_root,
        _sweep_dead_pid_cache_dirs,
    )

    root = _seatbelt_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    stray_file = root / "not-a-pid-dir.txt"
    stray_file.write_text("x", encoding="utf-8")
    stray_dir = root / "not-numeric"
    stray_dir.mkdir(exist_ok=True)

    try:
        _sweep_dead_pid_cache_dirs()  # must not raise
        assert stray_file.exists()
        assert stray_dir.exists()
    finally:
        stray_file.unlink(missing_ok=True)
        stray_dir.rmdir()


def test_sweep_runs_at_most_once_per_process():
    """Tier 2: the module-level guard — a second call in the same process
    is a no-op even if a fresh dead-pid subdirectory appears in between,
    matching the docstring's own "at most once per process" claim."""
    from reyn.security.sandbox.backends.seatbelt import (
        _seatbelt_cache_root,
        _sweep_dead_pid_cache_dirs,
    )

    _sweep_dead_pid_cache_dirs()  # first call — consumes the "once"

    root = _seatbelt_cache_root()
    dead = root / str(_dead_pid())
    dead.mkdir(parents=True, exist_ok=True)

    try:
        _sweep_dead_pid_cache_dirs()  # second call — must be a no-op
        assert dead.exists(), (
            "the sweep ran a second time in the same process — the "
            "module-level guard is not doing its job"
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(dead, ignore_errors=True)


def test_wrap_command_triggers_the_sweep_before_creating_its_own_pid_dir():
    """Tier 2: integration — a real ``wrap_command()`` cache-miss call
    triggers the sweep as a side effect (not just the unit-level direct
    call above), and a dead-pid sibling planted beforehand is gone
    afterward."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_root

    root = _seatbelt_cache_root()
    dead = root / str(_dead_pid())
    dead.mkdir(parents=True, exist_ok=True)

    backend = SeatbeltBackend()
    wrapped = backend.wrap_command(["/bin/echo", "hi"], SandboxPolicy(write_paths=[]))

    assert not dead.exists()

    wrapped.cleanup()
