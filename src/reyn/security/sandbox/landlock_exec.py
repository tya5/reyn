"""landlock_exec — a re-exec shim that restricts the current process under
Landlock, then execs a target command (FP-0017 / #1344 follow-up E).

Why a shim. A persistent stdio MCP server is a long-running subprocess, so the
backend's one-shot ``run()`` does not fit; the wrap must be at the COMMAND level
— mirroring the Seatbelt ``sandbox-exec -f <profile> cmd`` wrap in
``mcp_client._sandbox_wrap_stdio``. Landlock has no CLI wrapper, so this module
IS the wrapper::

    python -m reyn.security.sandbox.landlock_exec --policy <json> -- <command> <args...>

``main()`` applies ``LandlockBackend``'s ruleset to *itself* via ``apply()``,
loads the seccomp filter, then ``os.execvp()``s the target — which inherits both
irrevocable restrictions (they survive ``execve``). On a non-Linux / no-landlock
host the shim REFUSES to run (raises), so a caller never gets a false sense of
enforcement.

**This module used to duplicate the backend's ruleset build, and the copies
drifted** (#2980). The backend was ported to the real py-landlock porcelain
(#1693); this shim went on calling ``Ruleset.add_path_beneath_rule`` /
``restrict_self`` / ``add_net_port_rule`` — methods the pinned
``landlock==1.0.0.dev5`` does not define — so every launch through here raised
``AttributeError`` before restricting anything, and the MCP-stdio and CodeAct
seams ran unsandboxed for 41 days. Both seams now call ONE
:func:`~reyn.security.sandbox.backends.landlock.build_ruleset`; do not
reintroduce a second one here.

What kept that invisible is worth more than the fix: a TODO on the old build
named the exact check that would have caught it ("verify … for the installed
landlock package version"), the tests drove ``_apply_seccomp`` directly rather
than this module's entry point, and ``available()`` only ever asked whether the
package imported. The observation that closes it is
``reyn.security.sandbox.self_test``, which launches THROUGH ``wrap_command`` on
the host making the claim.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from .backends.seccomp import load_seccomp_filter, preload_native_dependency
from .policy import SandboxPolicy

_MODULE = "reyn.security.sandbox.landlock_exec"


def _policy_to_json(policy: SandboxPolicy) -> str:
    """Serialize a SandboxPolicy to a single JSON arg (round-trips via
    :func:`_policy_from_json`)."""
    return json.dumps(dataclasses.asdict(policy), separators=(",", ":"))


def _policy_from_json(s: str) -> SandboxPolicy:
    """Reconstruct a SandboxPolicy from the JSON produced by
    :func:`_policy_to_json`."""
    return SandboxPolicy(**json.loads(s))


def build_landlock_exec_argv(
    policy: SandboxPolicy, command: str, args: list[str]
) -> tuple[str, list[str]]:
    """Return ``(executable, argv)`` that runs ``command`` under this Landlock
    policy via the re-exec shim.

    Pure (no side effects) — the COMMAND-level wrap analog of the Seatbelt
    ``("sandbox-exec", ["-f", <profile>, command, *args])`` wrap. The returned
    ``executable`` is the current interpreter so the shim is import-resolvable
    in the same environment; ``--`` separates the shim args from the target.
    """
    return sys.executable, [
        "-m",
        _MODULE,
        "--policy",
        _policy_to_json(policy),
        "--",
        command,
        *args,
    ]


def _parse_args(argv: list[str]) -> tuple[SandboxPolicy, str, list[str]]:
    """Parse the shim's own argv into ``(policy, command, args)``.

    Structural-testable without Landlock (the enforcement is in
    :func:`_apply_landlock`)."""
    parser = argparse.ArgumentParser(prog=f"python -m {_MODULE}", add_help=False)
    parser.add_argument("--policy", required=True)
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    rest = list(ns.rest)
    # argparse.REMAINDER keeps the leading "--" separator — strip it.
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        parser.error("no target command after --")
    return _policy_from_json(ns.policy), rest[0], rest[1:]


def _apply_seccomp(policy: SandboxPolicy) -> None:
    """Load the seccomp-BPF filter into the CURRENT process under ``policy``.

    Irrevocable, and survives the ``os.execvp`` that :func:`main` issues next —
    which is why ``execve``/``execveat`` are baseline-allowed (denying them would
    kill the shim before it could exec the target at all, #2962).

    ⚠ No-op when ``policy.allow_subprocess`` is True. #3030 measured this as a
    live network-deny gap (the filter also carries the NETWORK gate —
    ``_NETWORK_SYSCALLS`` are allowlisted only when ``policy.network`` — so
    skipping the whole filter dropped it too, on the stdio-MCP default). That
    gap is now closed by :func:`_apply_landlock`'s
    ``isolate_network_namespace()`` step, which is NOT gated on
    ``allow_subprocess`` and does not depend on this filter loading at all — a
    namespace with no interfaces refuses network regardless of which syscall
    (or io_uring opcode) asks. This filter's own network allowlist is
    defense-in-depth on top of that boundary, not the boundary itself; whether
    the FORK gate belongs on the whole filter or only on the allowlist contents
    remains #2962's open, un-fixed design question — unrelated to network now
    that netns is the network boundary.
    """
    if policy.allow_subprocess:
        return
    load_seccomp_filter(policy)


def _apply_landlock(policy: SandboxPolicy) -> None:
    """Restrict the CURRENT process under ``policy`` via netns, Landlock, then
    seccomp.

    Raises ``RuntimeError`` if Landlock is unavailable on this host, or if
    ``policy.network`` is False and network-namespace isolation cannot be
    established — the shim must never exec the target UNRESTRICTED (that would
    be a silent escape) or with network reachable when the policy denies it.

    The ruleset comes from ``LandlockBackend``'s ``build_ruleset``: ONE builder
    for both seams, so this shim cannot again call methods the pinned package
    does not have while the backend calls the real ones (#2980).

    **The order of the steps below is the security property**, and most of the
    orderings silently produce no enforcement at all:

    0. ``isolate_network_namespace()`` — when ``policy.network`` is False, move
       this process into a fresh, interface-less network namespace BEFORE
       seccomp. This is the actual boundary for ``network: false`` (#3030): it
       does not depend on ``allow_subprocess``, and it is not a syscall-name
       denylist (io_uring included), so nothing later in this function can
       widen or narrow it. It must precede the seccomp step (``unshare`` is not
       in the seccomp allowlist, so it would be refused afterward); it has no
       ordering dependency on Landlock (it writes no ``/proc/self/*`` map — see
       ``backends/netns``).
    1. ``preload_native_dependency()`` — resolve pyseccomp's native libraries
       while this process can still reach the filesystem. Its import shells out
       and writes a temp file; run it after step 2 and Landlock denies it, the
       filter never loads, and ``allow_subprocess=False`` enforces nothing
       (#3020). The shim is ALWAYS a fresh process, so it can never inherit the
       import from a parent the way ``LandlockBackend.run``'s child accidentally
       could.
    2. ``ruleset.apply()`` — irrevocable, survives the ``execvp`` in
       :func:`main`.
    3. ``_apply_seccomp()`` — must come after 2, not before. Measured, not
       assumed: with the filter loaded first, ``apply()`` dies on
       ``landlock_restrict_self`` (``syscall(446, …) = -1``, EPERM) — the filter
       refuses the syscall Landlock's own setup needs, since it is not in the
       allowlist. Landlock would then be absent while the shim exec'd the target.
    """
    from .backends.landlock import LandlockBackend, build_ruleset

    backend = LandlockBackend()
    if not backend.available():
        raise RuntimeError(
            f"{_MODULE}: Landlock unavailable on this host "
            f"(import_error={backend.import_error!r}); refusing to exec the "
            "target unrestricted. Run on Linux 5.13+ with the `landlock` package."
        )

    if not policy.network:
        from .backends.netns import isolate_network_namespace

        isolate_network_namespace()  # raises RuntimeError -> fail-closed (#3030)

    ruleset = build_ruleset(policy, backend.abi_version or 0)
    if not policy.allow_subprocess:
        preload_native_dependency()
    ruleset.apply()  # type: ignore[attr-defined]
    _apply_seccomp(policy)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse argv, apply Landlock to self, exec the target.

    Returns a non-zero code only on failure; on success ``os.execvp`` replaces
    the process and never returns."""
    policy, command, args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        _apply_landlock(policy)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)  # noqa: T201
        return 2
    os.execvp(command, [command, *args])
    return 127  # unreachable on success (execvp replaces the process)


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main())
