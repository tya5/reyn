"""Tier 2: SeatbeltBackend invariants (FP-0017 Component C)."""
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
    `(allow mach-lookup)` — real measurement (architect, #4932) found this
    ONE service is what both `security`/Keychain and `gh auth status`
    (which shells out to `security` for its stored token) need and nothing
    else in the 9 candidate SBPL classes was required."""
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
    token) is the CI-portable witness for the underlying capability: it
    only needs to enumerate the machine's own keychain search list, not
    a specific credential.

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

    # cleanup() on a cached path must be a no-op (a second caller sharing
    # this policy still needs the file); confirmed by asserting it survives.
    wrapped1.cleanup()
    assert __import__("os").path.exists(path1)

    import os

    os.unlink(path1)


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
