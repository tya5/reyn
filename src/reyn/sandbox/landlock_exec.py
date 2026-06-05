"""landlock_exec — a re-exec shim that restricts the current process under
Landlock, then execs a target command (FP-0017 / #1344 follow-up E).

Why a shim. A persistent stdio MCP server is a long-running subprocess, so the
backend's one-shot ``run()`` does not fit; the wrap must be at the COMMAND level
— mirroring the Seatbelt ``sandbox-exec -f <profile> cmd`` wrap in
``mcp_client._sandbox_wrap_stdio``. Landlock has no CLI wrapper, so this module
IS the wrapper::

    python -m reyn.sandbox.landlock_exec --policy <json> -- <command> <args...>

``main()`` applies ``landlock.Ruleset().restrict_self()`` to *itself*, then
``os.execvp()``s the target — the target inherits the irrevocable restriction
(Landlock restrictions survive ``execve``).

Linux-validation-pending. The ruleset build + ``restrict_self()`` here MIRROR
``LandlockBackend.run`` and carry the SAME ``fp-0017-b`` "Linux validation
needed" TODOs (the maintainer dev environment is macOS-only). On a non-Linux /
no-landlock host the shim REFUSES to run (raises), so a caller never gets a
false sense of enforcement. When the backend's ruleset build is Linux-validated,
this and the backend should consolidate onto one shared ruleset builder; until
then they intentionally duplicate (each independently carries the unvalidated
caveat — no NEW unvalidated surface is introduced).

Deferred-validation plan (#1344): real end-to-end enforcement is validated on a
Linux host — reyn's own docker backend (#1324 launcher) can spin a Linux
container and exercise this shim's restrict_self() effects (fs/net actually
blocked). Tracked as a follow-up; not run here (these tests are structural +
skip-if-no-landlock).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from .policy import SandboxPolicy

_MODULE = "reyn.sandbox.landlock_exec"


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


def _apply_landlock(policy: SandboxPolicy) -> None:
    """Restrict the CURRENT process under ``policy`` via Landlock.

    Raises ``RuntimeError`` if Landlock is unavailable on this host — the shim
    must never exec the target UNRESTRICTED (that would be a silent escape).

    Mirrors ``LandlockBackend.run``'s ruleset build + ``restrict_self`` (same
    ``fp-0017-b`` Linux-validation TODOs)."""
    from .backends.landlock import LandlockBackend

    backend = LandlockBackend()
    if not backend.available():
        raise RuntimeError(
            f"{_MODULE}: Landlock unavailable on this host "
            f"(import_error={backend.import_error!r}); refusing to exec the "
            "target unrestricted. Run on Linux 5.13+ with the `landlock` package."
        )

    # TODO(fp-0017-b): Linux validation needed — verify Ruleset construction,
    # add_path_beneath_rule / add_net_port_rule API + access constants for the
    # installed landlock package version (mirrors LandlockBackend.run).
    import landlock  # noqa: PLC0415

    ruleset = landlock.Ruleset()  # type: ignore[attr-defined]
    # Broad read (#1199 / #1323): allowlist-only, so a single read rule on "/".
    # read_deny_paths is NOT expressible on Landlock (the network gate is the
    # exfil guard) — same residual-risk asymmetry as the backend.
    ruleset.add_path_beneath_rule("/", read_only=True)  # type: ignore[attr-defined]
    for path in policy.write_paths:
        ruleset.add_path_beneath_rule(path, read_only=False)  # type: ignore[attr-defined]
    if not policy.network and hasattr(ruleset, "add_net_port_rule"):
        ruleset.add_net_port_rule(deny_all_outbound=True)  # type: ignore[attr-defined]
    ruleset.restrict_self()  # type: ignore[attr-defined]

    if not policy.allow_subprocess:
        try:
            from .backends.seccomp import install_seccomp_filter  # noqa: PLC0415
        except ImportError:
            install_seccomp_filter = None  # type: ignore[assignment]
        if install_seccomp_filter is not None:
            install_seccomp_filter(policy)


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
