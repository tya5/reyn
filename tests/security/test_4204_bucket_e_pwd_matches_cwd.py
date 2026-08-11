"""Tier 2: #4204 bucket E — a sandboxed child's $PWD matches its real cwd.

A real shell resets $PWD to its own cwd at startup; a direct exec (no
shell in between, as every sandbox backend does) does not. The whole
parent env passes through unmodified (resolve_passthrough_env, #3901
PR-B ④), so a stale $PWD (reyn's own launch directory) would otherwise
reach a child whose ACTUAL cwd is the `cwd` param the backend was called
with (e.g. a subdirectory-launch's real project root, #4204 condition
①) — a tool that trusts $PWD instead of calling getcwd() sees the wrong
directory with no way to detect it.

Real subprocess throughout: NoopBackend.run() spawns a REAL python
interpreter that reads its own os.environ["PWD"] and prints it — the
only way to observe what a child ACTUALLY receives, not what we intended
to pass.
"""
from __future__ import annotations

import asyncio
import sys

from reyn.security.sandbox import SandboxPolicy
from reyn.security.sandbox.noop_backend import NoopBackend


def test_noop_backend_sets_pwd_to_match_the_real_cwd(tmp_path, monkeypatch) -> None:
    """Tier 2: NoopBackend.run(cwd=X) gives the child env["PWD"] == X, even
    when the reyn PROCESS's own os.environ["PWD"] (inherited from its
    launch shell) says something else entirely.

    STRIP-FALSIFY: removing the `env["PWD"] = cwd` line in
    NoopBackend.run (the pre-#4204 form) makes this go RED — the child
    would report the STALE parent PWD (or none at all), not tmp_path."""
    # Simulate reyn's own process having a stale $PWD from its launch shell
    # (a real, different directory from the sandboxed child's actual cwd).
    stale_launch_dir = str(tmp_path.parent)
    monkeypatch.setenv("PWD", stale_launch_dir)

    backend = NoopBackend()
    result = asyncio.run(
        backend.run(
            [sys.executable, "-c", "import os; print(os.environ.get('PWD', ''), end='')"],
            SandboxPolicy(),
            cwd=str(tmp_path),
        )
    )

    reported_pwd = result.stdout.decode()
    assert reported_pwd == str(tmp_path)
    assert reported_pwd != stale_launch_dir


def test_noop_backend_omits_pwd_override_when_no_cwd_given(tmp_path, monkeypatch) -> None:
    """Tier 2: when the caller passes no cwd at all (cwd=None), the child's
    $PWD is whatever the parent env already carried — no override is
    invented out of nowhere. Confirms the fix is conditional on cwd being
    known, not an unconditional PWD stamp."""
    monkeypatch.setenv("PWD", str(tmp_path))

    backend = NoopBackend()
    result = asyncio.run(
        backend.run(
            [sys.executable, "-c", "import os; print(os.environ.get('PWD', ''), end='')"],
            SandboxPolicy(),
            cwd=None,
        )
    )

    assert result.stdout.decode() == str(tmp_path)
