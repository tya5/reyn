"""Tier 2: Landlock re-exec shim for stdio MCP wrapping (#1344 follow-up E).

A persistent stdio MCP server can't use the backend's one-shot run(); on Linux
it is wrapped at the COMMAND level by re-execing through
``reyn.sandbox.landlock_exec``, which restricts itself then execs the target.

Scope of these tests is STRUCTURAL (the maintainer dev env is macOS-only):
  - the pure argv builder + policy JSON round-trip + arg parse (no Landlock);
  - the refuse-to-run guarantee when Landlock is unavailable (so the shim never
    execs the target unrestricted = no silent escape).
Real end-to-end ENFORCEMENT (restrict_self actually blocking fs/net) is
Linux-validation-pending — the same caveat the landlock backend carries
(fp-0017-b) — and is skipped where Landlock is unavailable.

No mocks — the real SandboxPolicy / real shim functions / a real subprocess.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reyn.sandbox.landlock_exec import (
    _MODULE,
    _parse_args,
    _policy_from_json,
    _policy_to_json,
    build_landlock_exec_argv,
)
from reyn.sandbox.policy import SandboxPolicy


def _landlock_available() -> bool:
    from reyn.sandbox.backends.landlock import LandlockBackend

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
    from reyn.sandbox.landlock_exec import _apply_landlock

    with pytest.raises(RuntimeError, match="Landlock unavailable"):
        _apply_landlock(SandboxPolicy())


@pytest.mark.skipif(
    _landlock_available(), reason="Landlock available — enforcement path Linux-validated separately"
)
def test_main_subprocess_refuses_when_unavailable():
    """Tier 2: run as a real subprocess on a no-Landlock host, the shim exits
    non-zero and does NOT exec the target (end-to-end of the refuse path)."""
    import os
    from pathlib import Path

    import reyn

    pol = SandboxPolicy()
    exe, argv = build_landlock_exec_argv(pol, "/bin/echo", ["SHOULD-NOT-RUN"])
    # Run the shim against the reyn under test (not a venv-installed copy) by
    # pinning PYTHONPATH to this checkout's src root (env-dependent path lesson).
    src_root = Path(reyn.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(src_root)}
    proc = subprocess.run(
        [exe, *argv], capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 2  # the refuse exit code
    assert "Landlock unavailable" in proc.stderr
    assert "SHOULD-NOT-RUN" not in proc.stdout  # the target never ran
