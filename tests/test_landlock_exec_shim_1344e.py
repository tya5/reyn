"""Tier 2: Landlock re-exec shim for stdio MCP wrapping (#1344 follow-up E).

A persistent stdio MCP server can't use the backend's one-shot run(); on Linux
it is wrapped at the COMMAND level by re-execing through
``reyn.security.sandbox.landlock_exec``, which restricts itself then execs the target.

Three groups, and the third is the one that matters:
  - the pure argv builder + policy JSON round-trip + arg parse (no Landlock);
  - the refuse-to-run guarantee where Landlock is ABSENT (so the shim never
    execs the target unrestricted = no silent escape);
  - **real enforcement, where Landlock is PRESENT** — the shim actually denies a
    write outside ``write_paths`` and a fork under ``allow_subprocess=False``.

That third group exists because its absence is what #2980 was. Every test here
used to be in the first two groups, and #2980's own title names the consequence:
*"its test bypasses the production entry point"*. The shim called ``Ruleset``
methods the pinned ``landlock==1.0.0.dev5`` does not define, so it raised
``AttributeError`` before restricting anything — and stayed green for 41 days,
because nothing drove ``_apply_landlock`` and the ruleset build carried a TODO
naming the exact check ("verify … for the installed landlock package version")
that nobody could run. A correct, predictive comment guarded nothing.

⚠ **The enforcement group SKIPS where Landlock is absent, which is macOS and
every job in ``test.yml``** (it omits the ``sandbox-linux`` extra — loading a real
filter is irrevocable, so pytest's shared session is the wrong shape for it). A
green run of this file on a dev box is therefore NOT evidence about enforcement —
read the skips.

What witnesses it: ``sandbox-landlock-deny-gate.yml`` (#2983 stage 3), which
installs the extra on a Linux runner and runs this file — plus, as its actual
gate, ``scripts/sandbox_landlock_deny_gate.py``, which drives the same two axes
through the production probes and cannot skip. That job witnesses ONE Landlock
ABI (the runner kernel's; an older ABI cannot be faked in a container), so ABI
1-2 — most of the installed base — is still covered only by
``LandlockBackend.self_test()`` failing closed on the operator's own host.

No mocks — the real SandboxPolicy / real shim functions / a real subprocess.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reyn.security.sandbox.landlock_exec import (
    _MODULE,
    _parse_args,
    _policy_from_json,
    _policy_to_json,
    build_landlock_exec_argv,
)
from reyn.security.sandbox.policy import SandboxPolicy


def _landlock_available() -> bool:
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    return LandlockBackend().available()


# ── pure: argv builder + policy round-trip + parse (no Landlock needed) ────────


def test_build_argv_is_python_shim():
    """Tier 2: the wrap is the current interpreter running the shim module, with
    the policy JSON and the target after ``--`` (COMMAND-level analog of the
    Seatbelt sandbox-exec wrap)."""
    pol = SandboxPolicy(network=False, write_paths=["/ws"])
    exe, argv = build_landlock_exec_argv(pol, "my-mcp", ["--flag", "x"])
    assert exe == sys.executable
    assert argv[:2] == ["-m", _MODULE]
    assert "--policy" in argv
    # the target command + args follow the "--" separator, unchanged
    sep = argv.index("--")
    assert argv[sep + 1:] == ["my-mcp", "--flag", "x"]


def test_policy_json_roundtrips_all_fields():
    """Tier 2: every policy field survives the JSON arg round-trip (so the shim
    enforces the operator's policy, not a lossy subset)."""
    pol = SandboxPolicy(
        network=True,
        read_paths=["/r"],
        write_paths=["/w"],
        read_deny_paths=["~/.ssh"],
        allow_subprocess=True,
        env_passthrough=["PATH", "HOME"],
        timeout_seconds=42,
    )
    assert _policy_from_json(_policy_to_json(pol)) == pol


def test_parse_args_recovers_policy_and_target():
    """Tier 2: _parse_args inverts build_landlock_exec_argv (the shim sees the
    same policy + target the wrap encoded). The leading ``-m <module>`` is
    consumed by python, so the shim's own argv is everything after it."""
    pol = SandboxPolicy(network=False, write_paths=["/ws"])
    _exe, argv = build_landlock_exec_argv(pol, "my-mcp", ["--flag"])
    shim_argv = argv[2:]  # drop ["-m", module] (python's args, not the shim's)
    parsed_pol, command, args = _parse_args(shim_argv)
    assert parsed_pol == pol
    assert command == "my-mcp"
    assert args == ["--flag"]


# ── refuse-to-run guarantee (no false enforcement) ────────────────────────────


@pytest.mark.skipif(
    _landlock_available(), reason="Landlock available — enforcement path Linux-validated separately"
)
def test_apply_landlock_refuses_when_unavailable():
    """Tier 2: where Landlock is unavailable, _apply_landlock RAISES — the shim
    must never exec the target unrestricted (no silent escape)."""
    from reyn.security.sandbox.landlock_exec import _apply_landlock

    with pytest.raises(RuntimeError, match="Landlock unavailable"):
        _apply_landlock(SandboxPolicy())


@pytest.mark.skipif(
    _landlock_available(), reason="Landlock available — enforcement path Linux-validated separately"
)
def test_main_subprocess_refuses_when_unavailable(out_of_process_reyn: str):
    """Tier 2: run as a real subprocess on a no-Landlock host, the shim exits
    non-zero and does NOT exec the target (end-to-end of the refuse path)."""
    pol = SandboxPolicy()
    exe, argv = build_landlock_exec_argv(pol, "/bin/echo", ["SHOULD-NOT-RUN"])
    # Run the shim against the reyn under test (not a venv-installed copy) by
    # pinning PYTHONPATH to this checkout's src root (env-dependent path lesson).
    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    proc = subprocess.run(
        [exe, *argv], capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 2  # the refuse exit code
    assert "Landlock unavailable" in proc.stderr
    assert "SHOULD-NOT-RUN" not in proc.stdout  # the target never ran


# ── real enforcement, where Landlock is PRESENT (#2980 / #3020) ───────────────
#
# These SKIP off Linux — see the module docstring. That skip is not a formality:
# it is the reason both defects survived, so a green run without them says
# nothing about enforcement.

requires_landlock = pytest.mark.skipif(
    not _landlock_available(),
    reason="Landlock unavailable — real enforcement cannot be witnessed on this host",
)


def _shim_run(
    src_root: str, policy: SandboxPolicy, argv: list[str]
) -> subprocess.CompletedProcess:
    """Launch *argv* through the backend's real ``wrap_command`` — the production
    seam (MCP stdio / CodeAct) — and return the finished process.

    PYTHONPATH is pinned to *src_root* (the ``out_of_process_reyn`` fixture
    value, as the refuse-path test above does) so the shim re-execs the reyn
    under test rather than an installed copy. Custom ``PATH`` preserved — the
    landlock test needs it.
    """
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    wrapped = LandlockBackend().wrap_command(argv, policy)
    return subprocess.run(
        wrapped.argv,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": src_root},
    )


@requires_landlock
def test_shim_denies_a_write_outside_write_paths(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2c: the shim actually restricts the filesystem — a write outside
    ``write_paths`` does not happen, while one inside it does.

    Both halves are load-bearing. Without the positive control, "the file is
    absent" is equally true of a shim that raised before exec — which is exactly
    what #2980 was, so a one-sided assertion here would have passed against the
    broken shim.

    The oracle is the filesystem, not the exit code: the file is the security
    property; the exit code is only a report of it.
    """
    touch = shutil.which("touch")
    assert touch, "no touch(1) on PATH"
    granted, denied = tmp_path / "granted", tmp_path / "denied"
    granted.mkdir()
    denied.mkdir()
    policy = SandboxPolicy(
        write_paths=[str(granted)], read_deny_paths=[], network=True,
        allow_subprocess=True,  # isolate the write axis from the syscall layer
    )

    control = granted / "control"
    _shim_run(out_of_process_reyn, policy, [touch, str(control)])
    assert control.exists(), (
        "the shim could not write to a path the policy GRANTS, so a denied write "
        "below would prove nothing (this is #2980's shape: the shim raising "
        "before it execs anything looks identical to a deny)"
    )

    escape = denied / "escape"
    proc = _shim_run(out_of_process_reyn, policy, [touch, str(escape)])
    assert not escape.exists(), (
        f"no deny fired: the shim wrote {escape}, outside the policy's only "
        f"write grant — it execs the target unrestricted (rc={proc.returncode}, "
        f"stderr={proc.stderr[:300]!r})"
    )


@requires_landlock
def test_shim_denies_a_fork_when_allow_subprocess_is_false(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2c: the shim's seccomp filter LOADS and denies process creation —
    the axis #3020 broke by importing pyseccomp after Landlock had restricted it.

    Three arms, because two different lies are available (mirrors
    ``self_test.probe_subprocess_enforcement``, whose reasoning this follows):

    1. a fork under ``allow_subprocess=True`` must succeed — else the probe
       cannot see a spawn at all;
    2. a NON-forking command under ``allow_subprocess=False`` must still run —
       else a filter refusing EVERYTHING (#2962, which killed /bin/echo) is
       indistinguishable from one refusing exactly ``fork``;
    3. only then: the fork under ``allow_subprocess=False`` must not happen.

    Arms 2 and 3 differ in nothing but the fork, so arm 3's absent marker is
    attributable to the fork rather than to a wrap that is simply dead.

    The command must be a PIPELINE: a shell asked to run one simple command may
    exec it in place with no fork at all, which would make arm 3 pass for the
    wrong reason.
    """
    sh, touch, cat = shutil.which("sh"), shutil.which("touch"), shutil.which("cat")
    assert sh and touch and cat, "need sh/touch/cat on PATH"
    granted = tmp_path / "granted"
    granted.mkdir()

    def policy(allow_subprocess: bool) -> SandboxPolicy:
        return SandboxPolicy(
            write_paths=[str(granted)], read_deny_paths=[], network=True,
            allow_subprocess=allow_subprocess,
        )

    def forking(marker: Path) -> list[str]:
        return [sh, "-c", f"{shlex.quote(touch)} {shlex.quote(str(marker))} "
                          f"| {shlex.quote(cat)}"]

    control = granted / "control-spawn"
    _shim_run(out_of_process_reyn, policy(True), forking(control))
    assert control.exists(), (
        "the shim could not spawn even with allow_subprocess=True, so a missing "
        "marker under False would prove nothing"
    )

    alive = granted / "control-nofork"
    proc = _shim_run(out_of_process_reyn, policy(False), [touch, str(alive)])
    assert alive.exists(), (
        f"under allow_subprocess=False the shim could not run even a NON-forking "
        f"command — it is failing wholesale rather than denying process creation "
        f"(rc={proc.returncode}, stderr={proc.stderr[:300]!r}). This is what "
        f"#3020 looked like: pyseccomp's import, deferred until after Landlock "
        f"applied, died on its own temp file"
    )

    spawned = granted / "spawned"
    proc = _shim_run(out_of_process_reyn, policy(False), forking(spawned))
    assert not spawned.exists(), (
        f"no subprocess deny fired: a command that must fork to run wrote "
        f"{spawned} under allow_subprocess=False — the seccomp filter is not "
        f"active (rc={proc.returncode}, stderr={proc.stderr[:300]!r})"
    )
