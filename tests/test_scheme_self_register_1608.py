"""Tier 2: #1608 ④ — built-in tool-use schemes self-register on import.

The OS scheme resolver no longer names any scheme class; each scheme module calls
``register_scheme`` at import time and the ``schemes`` package ``__init__`` imports
them all. The load-bearing invariant (sandbox_2's completeness axis): **all built-in
names resolve after importing only the package, with NO prior explicit scheme import
by the caller.** This MUST be checked in a FRESH interpreter — an in-process test
would false-pass because sibling tests already populate the global registry.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import reyn

# This test tree's src root — propagated to the subprocess so it imports the SAME
# reyn (the #1609 worktree-drift lesson: sys.executable's default reyn may resolve
# to a different worktree's venv).
_SRC = str(Path(reyn.__file__).resolve().parent.parent)


def _fresh_interpreter(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, env=env,
    )


def test_all_builtins_resolve_in_fresh_interpreter() -> None:
    """Tier 2: #1608 ④ — a fresh interpreter that imports ONLY the schemes package
    (no explicit scheme-class import) finds all 4 built-ins registered + resolvable,
    and the default is unchanged. This is the completeness gate."""
    result = _fresh_interpreter(
        """
        # The ONLY scheme-related import — must self-register the full built-in set.
        import reyn.tools.schemes  # noqa: F401
        from reyn.tools.scheme import (
            DEFAULT_SCHEME_NAME, get_scheme, registered_scheme_names,
        )
        expected = {"universal-category", "enumerate-all", "retrieval", "codeact"}
        names = set(registered_scheme_names())
        assert expected <= names, f"missing built-ins: {expected - names}"
        for n in expected:
            s = get_scheme(n)
            assert s is not None and s.name == n, n
        assert DEFAULT_SCHEME_NAME == "enumerate-all"
        assert get_scheme(DEFAULT_SCHEME_NAME) is not None
        print("RESOLVE_OK")
        """
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "RESOLVE_OK" in result.stdout


def test_resolver_finds_all_builtins_without_naming_them() -> None:
    """Tier 2: #1608 ④ — _resolve_tool_use_scheme (the OS resolver, which names NO
    scheme class) resolves each built-in name from a fresh interpreter, and an
    unknown name falls back to the default. Behaviour-invariant vs the old lazy loop."""
    result = _fresh_interpreter(
        """
        from reyn.runtime.router_loop import _resolve_tool_use_scheme
        for n in ("universal-category", "enumerate-all", "retrieval", "codeact"):
            s = _resolve_tool_use_scheme(n)
            assert s is not None and s.name == n, n
        # Unknown / None → default.
        assert _resolve_tool_use_scheme("no-such").name == "enumerate-all"
        assert _resolve_tool_use_scheme(None).name == "enumerate-all"
        print("RESOLVE_OK")
        """
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "RESOLVE_OK" in result.stdout
